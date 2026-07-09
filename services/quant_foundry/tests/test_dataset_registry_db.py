"""Tests for the dataset manifest registry (migration 0006 + ORM + verify script).

Tests verify:
  - The ORM model ``DatasetManifestRow`` can insert and query a dataset manifest
    row on an in-memory SQLite database.
  - The migration ``0006_dataset_manifests`` has the correct revision chain
    (``revision='0006'``, ``down_revision='0005'``) and creates the
    ``dataset_manifests`` table with all required columns on SQLite.
  - CHECK constraints enforce the ``readiness_level`` domain (L1-L4) and
    non-negative ``row_count``.
  - ``scripts/verify_dataset_manifest.py`` passes on a valid manifest + data
    file and fails (exit 1) on a tampered manifest.
  - The verify script is standalone (no quant_foundry imports at module level).

The tests follow the same pattern as ``test_registry_db.py``: in-memory SQLite
with generic JSON (not JSONB) so SQLite can render the columns.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import time
from typing import Any
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fincept_db.models import Base
from fincept_db.registry_tables import DatasetManifestRow

# ---------------------------------------------------------------------------
# Path setup — scripts/ is not a package, so add it to sys.path.
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    """In-memory SQLite engine with the dataset_manifests table.

    We create only the ``dataset_manifests`` table (no FK parents needed —
    this table has no foreign keys). Generic JSON type (not JSONB) so SQLite
    can render the ``purged_fold_spec`` column.
    """
    eng = create_engine("sqlite:///:memory:", future=True)

    @event.listens_for(eng, "connect")
    def _enable_fk(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng, tables=[DatasetManifestRow.__table__])
    yield eng
    eng.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest_row(
    manifest_id: str = "manifest-001",
    dataset_id: str = "ds-feature-lake-v1",
    manifest_hash: str | None = None,
    pit_proof_verified: bool = True,
    readiness_level: str = "L3",
    row_count: int = 1000,
) -> DatasetManifestRow:
    """Build a DatasetManifestRow with sensible defaults."""
    now_ns = time.time_ns()
    return DatasetManifestRow(
        manifest_id=manifest_id,
        dataset_id=dataset_id,
        manifest_hash=manifest_hash or "a" * 64,
        manifest_uri="file:///data/ds.manifest.json",
        data_uri="file:///data/ds.parquet",
        data_sha256="b" * 64,
        data_format="parquet",
        row_count=row_count,
        feature_schema_hash="f" * 64,
        label_schema_hash="l" * 64,
        readiness_level=readiness_level,
        pit_proof_verified=pit_proof_verified,
        purged_fold_spec={
            "schema_version": 1,
            "folds": [
                {
                    "fold_id": 0,
                    "train_start": 1000,
                    "train_end": 2000,
                    "val_start": 2100,
                    "val_end": 2500,
                    "purge_start": 2000,
                    "purge_end": 2100,
                }
            ],
            "embargo_ns": 500_000_000,
            "max_label_horizon_ns": 432_000_000,
        },
        embargo_length=500_000_000,
        quality_report_uri="file:///data/ds.quality.json",
        quality_report_sha256="q" * 64,
        created_at_ns=now_ns,
        updated_at_ns=now_ns,
    )


def _make_valid_manifest_dict(
    *,
    feature_names: tuple[str, ...] = ("ret_1d", "ret_5d", "vol_20d", "mom_10d", "vol_ratio"),
    row_count: int = 3,
    data_sha256: str | None = None,
    pit_proof_verified: bool = True,
    data_format: str = "csv",
    data_uri: str = "file:///data/ds.csv",
) -> dict[str, Any]:
    """Build a manifest dict with a correct manifest_hash for the verify script."""
    f_hash = hashlib.sha256(
        ":".join(sorted(feature_names)).encode("utf-8"),
    ).hexdigest()
    l_hash = hashlib.sha256(b"binary_forward_return_direction_5d").hexdigest()

    # Canonical payload fields (must match _CANONICAL_FIELDS in the script).
    payload = {
        "schema_version": 1,
        "dataset_id": "ds-test-001",
        "feature_schema_hash": f_hash,
        "label_schema_hash": l_hash,
        "as_of_ts": 1_700_000_000_000_000_000,
        "universe_hash": "u" * 64,
        "row_count": row_count,
        "checksum": "c" * 64,
        "folds": {
            "schema_version": 1,
            "folds": [
                {
                    "schema_version": 1,
                    "fold_id": 0,
                    "train_start": 1000,
                    "train_end": 2000,
                    "val_start": 2100,
                    "val_end": 2500,
                    "purge_start": 2000,
                    "purge_end": 2100,
                }
            ],
            "embargo_ns": 500_000_000,
            "max_label_horizon_ns": 432_000_000,
        },
        "pit_proof_verified": pit_proof_verified,
        "source_vintage_refs": [],
        "quality_report_hash": None,
        "manifest_uri": "file:///data/ds.manifest.json",
        "data_uri": data_uri,
        "data_format": data_format,
        "data_sha256": data_sha256,
        "quality_report_uri": None,
        "quality_report_sha256": None,
    }
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    manifest_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

    body = dict(payload)
    body["manifest_hash"] = manifest_hash
    body["feature_names"] = list(feature_names)
    return body


# ---------------------------------------------------------------------------
# ORM model tests
# ---------------------------------------------------------------------------


class TestDatasetManifestRowORM:
    def test_insert_and_query(self, engine) -> None:
        """The ORM model can insert and query a dataset manifest row."""
        row = _make_manifest_row()
        with Session(engine) as session:
            session.add(row)
            session.commit()

            queried = session.scalars(
                select(DatasetManifestRow).where(DatasetManifestRow.manifest_id == "manifest-001")
            ).one()

        assert queried.manifest_id == "manifest-001"
        assert queried.dataset_id == "ds-feature-lake-v1"
        assert queried.manifest_hash == "a" * 64
        assert queried.manifest_uri == "file:///data/ds.manifest.json"
        assert queried.data_uri == "file:///data/ds.parquet"
        assert queried.data_sha256 == "b" * 64
        assert queried.data_format == "parquet"
        assert queried.row_count == 1000
        assert queried.feature_schema_hash == "f" * 64
        assert queried.label_schema_hash == "l" * 64
        assert queried.readiness_level == "L3"
        assert queried.pit_proof_verified is True
        assert queried.purged_fold_spec["embargo_ns"] == 500_000_000
        assert queried.embargo_length == 500_000_000
        assert queried.quality_report_uri == "file:///data/ds.quality.json"
        assert queried.quality_report_sha256 == "q" * 64
        assert queried.created_at_ns > 0
        assert queried.updated_at_ns > 0

    def test_insert_minimal_row(self, engine) -> None:
        """A row with only required fields (nullable fields omitted) inserts."""
        now_ns = time.time_ns()
        row = DatasetManifestRow(
            manifest_id="manifest-min",
            dataset_id="ds-min",
            manifest_hash="m" * 64,
            row_count=10,
            feature_schema_hash="f" * 64,
            label_schema_hash="l" * 64,
            readiness_level="L1",
            pit_proof_verified=False,
            purged_fold_spec={},
            created_at_ns=now_ns,
            updated_at_ns=now_ns,
        )
        with Session(engine) as session:
            session.add(row)
            session.commit()

            queried = session.scalars(
                select(DatasetManifestRow).where(DatasetManifestRow.manifest_id == "manifest-min")
            ).one()

        assert queried.manifest_uri is None
        assert queried.data_uri is None
        assert queried.data_sha256 is None
        assert queried.data_format is None
        assert queried.embargo_length is None
        assert queried.quality_report_uri is None
        assert queried.quality_report_sha256 is None
        assert queried.pit_proof_verified is False
        assert queried.readiness_level == "L1"

    def test_query_by_dataset_id(self, engine) -> None:
        """Multiple manifests for the same dataset_id can be queried."""
        for i in range(3):
            row = _make_manifest_row(
                manifest_id=f"manifest-{i}",
                manifest_hash=chr(ord("a") + i) * 64,
            )
            with Session(engine) as session:
                session.add(row)
                session.commit()

        with Session(engine) as session:
            rows = session.scalars(
                select(DatasetManifestRow)
                .where(DatasetManifestRow.dataset_id == "ds-feature-lake-v1")
                .order_by(DatasetManifestRow.manifest_id)
            ).all()

        assert len(rows) == 3
        assert [r.manifest_id for r in rows] == ["manifest-0", "manifest-1", "manifest-2"]

    def test_bad_readiness_level_rejected(self, engine) -> None:
        """CHECK constraint rejects an invalid readiness_level."""
        row = _make_manifest_row(readiness_level="L5")
        with Session(engine) as session:
            session.add(row)
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

    def test_negative_row_count_rejected(self, engine) -> None:
        """CHECK constraint rejects a negative row_count."""
        row = _make_manifest_row(row_count=-1)
        with Session(engine) as session:
            session.add(row)
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

    def test_duplicate_dataset_id_manifest_hash_rejected(self, engine) -> None:
        """Unique constraint rejects duplicate (dataset_id, manifest_hash)."""
        row1 = _make_manifest_row(manifest_id="m1", manifest_hash="d" * 64)
        row2 = _make_manifest_row(manifest_id="m2", manifest_hash="d" * 64)
        with Session(engine) as session:
            session.add(row1)
            session.commit()
            session.add(row2)
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------


class TestMigration0006:
    def test_revision_chain(self) -> None:
        """Migration 0006 has revision='0006' and down_revision='0005'."""
        import importlib

        mod = importlib.import_module("fincept_db.migrations.versions.0006_dataset_manifests")
        assert mod.revision == "0006"
        assert mod.down_revision == "0005"

    def test_migration_creates_table_on_sqlite(self) -> None:
        """The migration upgrade() creates dataset_manifests on SQLite.

        We run the migration's ``upgrade()`` through an Alembic
        ``MigrationContext`` configured for SQLite. ``JSONB`` is patched to
        ``JSON`` and the ``::jsonb`` server_default cast is neutralised so
        SQLite can execute the DDL.
        """
        import importlib

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        mod = importlib.import_module("fincept_db.migrations.versions.0006_dataset_manifests")

        eng = create_engine("sqlite:///:memory:", future=True)
        with eng.begin() as conn:
            ctx = MigrationContext.configure(conn)
            ops = Operations(ctx)

            # Patch: JSONB → JSON (SQLite has no JSONB), and op → our ops ctx.
            with patch.object(mod, "op", ops), patch.object(mod, "JSONB", sa.JSON):
                # Also neutralise the '::jsonb' server_default text that
                # SQLite cannot parse. We wrap sa.text to strip the cast.
                original_text = sa.text

                def _text(value: str):
                    cleaned = value.replace("::jsonb", "").replace("::json", "")
                    return original_text(cleaned)

                # Patch sa.text in the module namespace so the migration's
                # sa.text(...) calls use our cleaned version.
                with patch.object(sa, "text", _text):
                    mod.upgrade()

        # Verify the table exists with all expected columns.
        inspector = inspect(eng)
        assert "dataset_manifests" in inspector.get_table_names()

        columns = {c["name"]: c for c in inspector.get_columns("dataset_manifests")}
        expected_cols = {
            "manifest_id",
            "dataset_id",
            "manifest_hash",
            "manifest_uri",
            "data_uri",
            "data_sha256",
            "data_format",
            "row_count",
            "feature_schema_hash",
            "label_schema_hash",
            "readiness_level",
            "pit_proof_verified",
            "purged_fold_spec",
            "embargo_length",
            "quality_report_uri",
            "quality_report_sha256",
            "created_at_ns",
            "updated_at_ns",
        }
        assert expected_cols.issubset(set(columns.keys())), (
            f"missing columns: {expected_cols - set(columns.keys())}"
        )

        # Verify primary key.
        pk = inspector.get_pk_constraint("dataset_manifests")
        assert pk["constrained_columns"] == ["manifest_id"]

        # Verify indexes.
        indexes = {idx["name"] for idx in inspector.get_indexes("dataset_manifests")}
        assert "ix_dataset_manifests_dataset_id" in indexes
        assert "ix_dataset_manifests_readiness_level" in indexes
        assert "ix_dataset_manifests_manifest_hash" in indexes

    def test_migration_downgrade_drops_table(self) -> None:
        """The migration downgrade() drops dataset_manifests."""
        import importlib

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        mod = importlib.import_module("fincept_db.migrations.versions.0006_dataset_manifests")

        eng = create_engine("sqlite:///:memory:", future=True)
        with eng.begin() as conn:
            ctx = MigrationContext.configure(conn)
            ops = Operations(ctx)
            with patch.object(mod, "op", ops), patch.object(mod, "JSONB", sa.JSON):
                original_text = sa.text

                def _text(value: str):
                    return original_text(value.replace("::jsonb", ""))

                with patch.object(sa, "text", _text):
                    mod.upgrade()
                    mod.downgrade()

        inspector = inspect(eng)
        assert "dataset_manifests" not in inspector.get_table_names()


# ---------------------------------------------------------------------------
# verify_dataset_manifest.py script tests
# ---------------------------------------------------------------------------


class TestVerifyDatasetManifestScript:
    """Tests for the verify_dataset_manifest.py script.

    These tests use CSV data files (pandas is available; polars/pyarrow are
    not required). The verify script supports both parquet and csv.
    """

    def _write_data_csv(self, path: pathlib.Path, n_rows: int) -> str:
        """Write a small CSV file with n_rows and return its SHA-256."""
        import pandas as pd

        df = pd.DataFrame(
            {
                "decision_time": list(range(n_rows)),
                "ret_1d": [0.1] * n_rows,
                "ret_5d": [0.2] * n_rows,
                "vol_20d": [0.3] * n_rows,
                "mom_10d": [0.4] * n_rows,
                "vol_ratio": [1.0] * n_rows,
                "label": [1.0] * n_rows,
            }
        )
        df.to_csv(str(path), index=False)
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _write_manifest(
        self,
        path: pathlib.Path,
        manifest_dict: dict[str, Any],
    ) -> None:
        path.write_text(
            json.dumps(manifest_dict, sort_keys=True, indent=2),
            encoding="utf-8",
        )

    def test_valid_manifest_passes(self, tmp_path: pathlib.Path) -> None:
        """verify_dataset_manifest exits 0 on a valid manifest + data file."""
        import verify_dataset_manifest as vdm

        data_path = tmp_path / "data.csv"
        data_sha = self._write_data_csv(data_path, n_rows=3)

        manifest = _make_valid_manifest_dict(row_count=3, data_sha256=data_sha)
        manifest_path = tmp_path / "manifest.json"
        self._write_manifest(manifest_path, manifest)

        rc = vdm.main(["--manifest-path", str(manifest_path), "--data-path", str(data_path)])
        assert rc == 0

    def test_tampered_manifest_hash_fails(self, tmp_path: pathlib.Path) -> None:
        """verify_dataset_manifest exits 1 when manifest_hash is tampered."""
        import verify_dataset_manifest as vdm

        data_path = tmp_path / "data.csv"
        data_sha = self._write_data_csv(data_path, n_rows=3)

        manifest = _make_valid_manifest_dict(row_count=3, data_sha256=data_sha)
        # Tamper: change the manifest_hash to a wrong value.
        manifest["manifest_hash"] = "0" * 64
        manifest_path = tmp_path / "manifest.json"
        self._write_manifest(manifest_path, manifest)

        rc = vdm.main(["--manifest-path", str(manifest_path), "--data-path", str(data_path)])
        assert rc == 1

    def test_tampered_data_sha256_fails(self, tmp_path: pathlib.Path) -> None:
        """verify_dataset_manifest exits 1 when data_sha256 is wrong."""
        import verify_dataset_manifest as vdm

        data_path = tmp_path / "data.csv"
        self._write_data_csv(data_path, n_rows=3)

        manifest = _make_valid_manifest_dict(row_count=3, data_sha256="e" * 64)
        manifest_path = tmp_path / "manifest.json"
        self._write_manifest(manifest_path, manifest)

        rc = vdm.main(["--manifest-path", str(manifest_path), "--data-path", str(data_path)])
        assert rc == 1

    def test_wrong_row_count_fails(self, tmp_path: pathlib.Path) -> None:
        """verify_dataset_manifest exits 1 when row_count doesn't match."""
        import verify_dataset_manifest as vdm

        data_path = tmp_path / "data.csv"
        data_sha = self._write_data_csv(data_path, n_rows=3)

        # Manifest says 100 rows but data has 3.
        manifest = _make_valid_manifest_dict(row_count=100, data_sha256=data_sha)
        manifest_path = tmp_path / "manifest.json"
        self._write_manifest(manifest_path, manifest)

        rc = vdm.main(["--manifest-path", str(manifest_path), "--data-path", str(data_path)])
        assert rc == 1

    def test_pit_proof_false_fails(self, tmp_path: pathlib.Path) -> None:
        """verify_dataset_manifest exits 1 when pit_proof_verified is False."""
        import verify_dataset_manifest as vdm

        data_path = tmp_path / "data.csv"
        data_sha = self._write_data_csv(data_path, n_rows=3)

        manifest = _make_valid_manifest_dict(
            row_count=3, data_sha256=data_sha, pit_proof_verified=False
        )
        manifest_path = tmp_path / "manifest.json"
        self._write_manifest(manifest_path, manifest)

        rc = vdm.main(["--manifest-path", str(manifest_path), "--data-path", str(data_path)])
        assert rc == 1

    def test_tampered_feature_schema_hash_fails(self, tmp_path: pathlib.Path) -> None:
        """verify_dataset_manifest exits 1 when feature_schema_hash is wrong."""
        import verify_dataset_manifest as vdm

        data_path = tmp_path / "data.csv"
        data_sha = self._write_data_csv(data_path, n_rows=3)

        manifest = _make_valid_manifest_dict(row_count=3, data_sha256=data_sha)
        # Tamper: change feature_schema_hash to a wrong value.
        manifest["feature_schema_hash"] = "z" * 64
        manifest_path = tmp_path / "manifest.json"
        self._write_manifest(manifest_path, manifest)

        rc = vdm.main(["--manifest-path", str(manifest_path), "--data-path", str(data_path)])
        assert rc == 1

    def test_missing_manifest_file_fails(self, tmp_path: pathlib.Path) -> None:
        """verify_dataset_manifest exits 1 when the manifest file is missing."""
        import verify_dataset_manifest as vdm

        data_path = tmp_path / "data.csv"
        self._write_data_csv(data_path, n_rows=3)

        rc = vdm.main(
            [
                "--manifest-path",
                str(tmp_path / "nonexistent.json"),
                "--data-path",
                str(data_path),
            ]
        )
        assert rc == 1

    def test_script_is_standalone(self) -> None:
        """The verify script must NOT import from quant_foundry."""
        import ast

        import verify_dataset_manifest as vdm

        # Check the module's source for actual quant_foundry import statements

        source = pathlib.Path(vdm.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("quant_foundry"), (
                        f"verify_dataset_manifest.py imports {alias.name} — must be standalone"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert node.module is None or not node.module.startswith("quant_foundry"), (
                    f"verify_dataset_manifest.py imports from {node.module} — must be standalone"
                )
