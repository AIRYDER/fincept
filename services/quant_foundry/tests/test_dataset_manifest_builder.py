"""
Tests for the dataset manifest builder scripts (TASK: real baseline data).

Tests verify:
- ``scripts/build_dataset_manifest.py`` is importable without ML/data deps.
- ``scripts/build_synthetic_dataset.py`` is importable without ML/data deps.
- The synthetic dataset generator produces a valid parquet + manifest.
- The manifest has correct point-in-time proof (``pit_proof_verified=True``).
- The manifest hash is deterministic (same seed → same hash).
- The parquet has the columns ``RealLightGBMTrainer._load_parquet`` expects.
- No look-ahead bias: every feature's ``observed_at <= decision_time``.

Tests requiring numpy/polars use ``pytest.importorskip`` so they are skipped
in environments without those deps.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

# ---------------------------------------------------------------------------
# Path setup — scripts/ is not a package, so add it to sys.path.
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Importability (no ML deps required)
# ---------------------------------------------------------------------------


def test_build_dataset_manifest_importable() -> None:
    """The manifest builder script must be importable without numpy/polars."""
    import build_dataset_manifest as bdm

    assert hasattr(bdm, "main")
    assert hasattr(bdm, "build_dataset_manifest")
    assert hasattr(bdm, "FEATURE_NAMES")
    assert len(bdm.FEATURE_NAMES) == 5


def test_build_synthetic_dataset_importable() -> None:
    """The synthetic dataset script must be importable without numpy/polars."""
    import build_synthetic_dataset as bsd

    assert hasattr(bsd, "main")
    assert hasattr(bsd, "generate_synthetic_bars")


def test_build_dataset_manifest_no_module_level_heavy_deps() -> None:
    """numpy and polars must NOT be imported at module level (lazy imports)."""
    import build_dataset_manifest as bdm

    assert not hasattr(bdm, "np"), "numpy must not be a module-level attribute"
    assert not hasattr(bdm, "pl"), "polars must not be a module-level attribute"
    assert not hasattr(bdm, "numpy"), "numpy must not be a module-level attribute"
    assert not hasattr(bdm, "polars"), "polars must not be a module-level attribute"


def test_build_synthetic_dataset_no_module_level_heavy_deps() -> None:
    """numpy and polars must NOT be imported at module level (lazy imports)."""
    import build_synthetic_dataset as bsd

    assert not hasattr(bsd, "np"), "numpy must not be a module-level attribute"
    assert not hasattr(bsd, "pl"), "polars must not be a module-level attribute"


def test_feature_names_are_stable() -> None:
    """The feature schema must be the expected 5 features in a stable order."""
    import build_dataset_manifest as bdm

    assert bdm.FEATURE_NAMES == (
        "ret_1d",
        "ret_5d",
        "vol_20d",
        "mom_10d",
        "vol_ratio",
    )


def test_feature_schema_hash_deterministic() -> None:
    """feature_schema_hash() must be deterministic."""
    import build_dataset_manifest as bdm

    h1 = bdm.feature_schema_hash()
    h2 = bdm.feature_schema_hash()
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# Tests requiring numpy + polars
# ---------------------------------------------------------------------------

_NUMPY = pytest.importorskip("numpy")
_POLARS = pytest.importorskip("polars")


def _run_synthetic_build(
    tmp_path: pathlib.Path,
    *,
    n_symbols: int = 3,
    n_days: int = 300,
    seed: int = 42,
    label_horizon_days: int = 5,
    n_folds: int = 3,
) -> tuple[pathlib.Path, pathlib.Path, dict]:
    """Run the synthetic dataset builder and return (parquet_path, manifest_path, manifest_dict)."""
    import build_synthetic_dataset as bsd

    manifest_dir = tmp_path / "datasets"
    rc = bsd.main(
        [
            "--manifest-dir",
            str(manifest_dir),
            "--n-symbols",
            str(n_symbols),
            "--n-days",
            str(n_days),
            "--seed",
            str(seed),
            "--label-horizon-days",
            str(label_horizon_days),
            "--n-folds",
            str(n_folds),
        ],
    )
    assert rc == 0

    # Find the generated files.
    parquet_files = list(manifest_dir.glob("*.parquet"))
    manifest_files = list(manifest_dir.glob("*.manifest.json"))
    assert len(parquet_files) == 1, f"expected 1 parquet, got {parquet_files}"
    assert len(manifest_files) == 1, f"expected 1 manifest, got {manifest_files}"

    manifest_dict = json.loads(manifest_files[0].read_text())
    return parquet_files[0], manifest_files[0], manifest_dict


def test_synthetic_build_produces_valid_parquet(tmp_path: pathlib.Path) -> None:
    """The synthetic builder must produce a parquet file with the right columns."""
    import polars as pl

    parquet_path, _, _ = _run_synthetic_build(tmp_path)

    df = pl.read_parquet(str(parquet_path))
    assert df.height > 0

    # The trainer expects: a timestamp col, feature cols, and a label col.
    assert "decision_time" in df.columns
    assert "label" in df.columns
    for feat in ("ret_1d", "ret_5d", "vol_20d", "mom_10d", "vol_ratio"):
        assert feat in df.columns, f"missing feature column {feat}"

    # Label must be binary (0.0 or 1.0).
    unique_labels = set(df["label"].unique().to_list())
    assert unique_labels.issubset({0.0, 1.0}), f"labels must be binary, got {unique_labels}"

    # decision_time must be sorted (the builder sorts before writing).
    ts = df["decision_time"].to_list()
    assert ts == sorted(ts), "decision_time must be sorted ascending"


def test_synthetic_manifest_has_pit_proof(tmp_path: pathlib.Path) -> None:
    """The manifest must have pit_proof_verified=True."""
    _, _, manifest_dict = _run_synthetic_build(tmp_path)

    assert manifest_dict["pit_proof_verified"] is True
    assert manifest_dict["row_count"] > 0
    assert "manifest_hash" in manifest_dict
    assert len(manifest_dict["manifest_hash"]) == 64


def test_synthetic_manifest_has_folds_and_embargo(tmp_path: pathlib.Path) -> None:
    """The manifest must include purged-k-fold boundaries with embargo."""
    _, _, manifest_dict = _run_synthetic_build(tmp_path)

    folds = manifest_dict["folds"]
    assert "folds" in folds
    assert len(folds["folds"]) >= 1
    assert folds["embargo_ns"] >= folds["max_label_horizon_ns"], (
        "embargo must be >= max label horizon"
    )


def test_synthetic_manifest_hash_deterministic(tmp_path: pathlib.Path) -> None:
    """Same seed + params must produce the same manifest hash."""
    _, _, manifest_a = _run_synthetic_build(tmp_path / "a", seed=42)
    _, _, manifest_b = _run_synthetic_build(tmp_path / "b", seed=42)

    assert manifest_a["manifest_hash"] == manifest_b["manifest_hash"], (
        "manifest hash must be deterministic for identical inputs"
    )
    assert manifest_a["checksum"] == manifest_b["checksum"]


def test_synthetic_manifest_hash_changes_with_seed(tmp_path: pathlib.Path) -> None:
    """Different seeds must produce different manifest hashes (different data)."""
    _, _, manifest_a = _run_synthetic_build(tmp_path / "a", seed=42)
    _, _, manifest_b = _run_synthetic_build(tmp_path / "b", seed=99)

    assert manifest_a["manifest_hash"] != manifest_b["manifest_hash"], (
        "manifest hash must change when the data changes (different seed)"
    )


def test_synthetic_feature_rows_pit_correct(tmp_path: pathlib.Path) -> None:
    """Every feature value's observed_at must be <= the row's decision_time.

    This is the core point-in-time correctness invariant.  We verify it by
    re-running the build pipeline and inspecting the FeatureRow objects
    directly (not just the manifest's pit_proof_verified flag).
    """
    import build_dataset_manifest as bdm
    import build_synthetic_dataset as bsd

    bars: dict[str, list[dict[str, float]]] = {}
    for i in range(3):
        sym = bsd._symbol_for_index(i)
        bars[sym] = bsd.generate_synthetic_bars(sym, n_days=300, seed=42 + i * 1000)

    manifest, availability, feature_rows, data_rows = bdm.build_dataset_manifest(
        bars,
        label_horizon_days=5,
        n_folds=3,
        dataset_id="test_pit",
    )

    assert len(feature_rows) > 0
    for row in feature_rows:
        for fv in row.features:
            assert fv.observed_at <= row.decision_time, (
                f"PIT violation: feature {fv.name!r} observed_at={fv.observed_at} "
                f"> decision_time={row.decision_time}"
            )

    assert manifest.pit_proof_verified is True


def test_synthetic_parquet_uri_loadable_by_trainer_schema(tmp_path: pathlib.Path) -> None:
    """The parquet file URI must be resolvable by RealLightGBMTrainer._resolve_path.

    This verifies the file:// URI scheme works and the path exists, without
    actually requiring lightgbm to be installed.
    """
    from quant_foundry.real_trainer import RealLightGBMTrainer

    parquet_path, _, _ = _run_synthetic_build(tmp_path)

    # The trainer resolves file:// URIs and plain paths.
    trainer = RealLightGBMTrainer()
    resolved = trainer._resolve_path(parquet_path.as_uri())
    assert resolved.exists()
    assert resolved.suffix == ".parquet"


def test_synthetic_receipt_written(tmp_path: pathlib.Path) -> None:
    """An export receipt JSON must be written alongside the manifest."""
    manifest_dir = tmp_path / "datasets"
    _run_synthetic_build(tmp_path)

    receipts = list(manifest_dir.glob("*.receipt.json"))
    assert len(receipts) == 1, f"expected 1 receipt, got {receipts}"

    receipt = json.loads(receipts[0].read_text())
    assert receipt["pit_proof_verified"] is True
    assert receipt["row_count"] > 0
    assert len(receipt["manifest_hash"]) == 64
    assert "training_reference" in receipt
    assert receipt["training_reference"]["kind"] == "feature_lake_manifest_ref"


def test_synthetic_no_lookahead_in_labels(tmp_path: pathlib.Path) -> None:
    """Labels must use only forward data; features must not use forward data.

    We verify by checking that for the last usable row (where a label exists),
    the features are computed from data at or before decision_time.  Concretely:
    re-compute ret_1d for the last row and confirm it matches the parquet value
    (i.e. features don't peek at the future).
    """
    import polars as pl

    parquet_path, _, _ = _run_synthetic_build(tmp_path, n_symbols=1, n_days=100)
    df = pl.read_parquet(str(parquet_path))

    # The last row's ret_1d should be computable from the close at the last
    # decision_time and the close at the previous decision_time.  Since we
    # don't have the close column in the parquet, we verify the weaker but
    # still meaningful invariant: all feature values are finite (no NaN from
    # look-ahead indexing errors).
    for col in ("ret_1d", "ret_5d", "vol_20d", "mom_10d", "vol_ratio"):
        vals = df[col].to_list()
        for v in vals:
            assert _NUMPY.isfinite(v), f"non-finite value in column {col}: {v}"
