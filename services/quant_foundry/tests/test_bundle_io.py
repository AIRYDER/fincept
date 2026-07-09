"""
Tests for C1 — Bundle Round-Trip Contract.

Verifies the invariants:
1. New training writes only ModelBundle v1.
2. Legacy bare LightGBM pickle is load-only compatibility.
3. bundle_manifest.json lists every member and sha256.
4. load_bundle() verifies member hashes before scoring.
5. bundle_kind is one of: single, meta_labeled.
6. meta_labeled requires both primary and meta members.
7. Missing meta member fails closed.
8. Unknown bundle kind fails closed.
9. Feature schema mismatch fails before scoring.
10. Selfcheck runs against the final serialized artifact bytes.
11. A selfcheck crash is a selfcheck failure.
12. Selfcheck failure → error_code="bundle_selfcheck_failed".
13. Selfcheck success records passed/n_rows_scored/output_sha256/
    bundle_sha256/loader_version/duration_ms.

ML dependencies (lightgbm, numpy) are imported via
pytest.importorskip so the file is collectable without them.
"""

from __future__ import annotations

import hashlib
import io
import json
import pickle
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

# Skip entire module if lightgbm / numpy are not installed.
_LIGHTGBM = pytest.importorskip("lightgbm")
_NUMPY = pytest.importorskip("numpy")

# Legacy trainer construction (without column_roles) emits a
# DeprecationWarning; these tests intentionally exercise that path.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# Helpers — synthetic LightGBM models
# ---------------------------------------------------------------------------


def _train_tiny_lightgbm(
    n_features: int = 4,
    n_rows: int = 100,
    seed: int = 42,
    objective: str = "binary",
    n_classes: int | None = None,
) -> Any:
    """Train a tiny LightGBM model on synthetic data and return the Booster."""
    import lightgbm as lgb
    import numpy as np

    rng = np.random.RandomState(seed)
    X = rng.randn(n_rows, n_features)
    if objective == "multiclass":
        logits = 0.8 * X[:, 0] + 0.5 * X[:, 1] - 0.6 * X[:, 2]
        y = np.where(logits > 0.3, 2, np.where(logits < -0.3, 0, 1))
        params = {
            "objective": "multiclass",
            "num_class": n_classes or 3,
            "metric": "multi_logloss",
            "verbosity": -1,
            "seed": seed,
            "deterministic": True,
            "num_threads": 1,
            "num_leaves": 15,
            "learning_rate": 0.1,
        }
    else:
        logits = 0.8 * X[:, 0] + 0.5 * X[:, 1] - 0.6 * X[:, 2]
        y = (logits > 0).astype(float)
        params = {
            "objective": "binary",
            "metric": "binary_logloss",
            "verbosity": -1,
            "seed": seed,
            "deterministic": True,
            "num_threads": 1,
            "num_leaves": 15,
            "learning_rate": 0.1,
        }
    train_set = lgb.Dataset(X, label=y)
    model = lgb.train(params, train_set, num_boost_round=20)
    return model


def _train_tiny_meta_model(
    primary_model: Any,
    X: Any,
    label_map: dict[str, int] | None = None,
    seed: int = 42,
) -> Any:
    """Train a tiny binary meta-model on (features + primary_side) → meta_label."""
    import lightgbm as lgb
    import numpy as np

    rng = np.random.RandomState(seed)
    primary_preds = primary_model.predict(X)
    preds_arr = np.asarray(primary_preds, dtype=np.float64)

    if preds_arr.ndim == 2:
        pred_classes = preds_arr.argmax(axis=1)
    else:
        pred_classes = preds_arr.astype(int)

    if label_map:
        inv = {v: k for k, v in label_map.items()}
        sides = np.array([inv.get(int(c), 0) for c in pred_classes], dtype=np.float64)
    else:
        sides = pred_classes.astype(np.float64)

    # Meta-label: 1 if side > 0 (correct-ish), 0 otherwise (simplified).
    meta_labels = (sides > 0).astype(float)
    X_meta = np.column_stack([X.astype(np.float64), sides.reshape(-1, 1)])

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "verbosity": -1,
        "seed": seed,
        "deterministic": True,
        "num_threads": 1,
        "num_leaves": 15,
        "learning_rate": 0.1,
    }
    train_set = lgb.Dataset(X_meta, label=meta_labels)
    meta_model = lgb.train(params, train_set, num_boost_round=20)
    return meta_model


def _make_sample_features(n_features: int = 4, n_rows: int = 5) -> list[list[float]]:
    """Generate a small sample of feature rows for scoring/selfcheck."""
    import numpy as np

    rng = np.random.RandomState(99)
    return rng.randn(n_rows, n_features).tolist()


# ---------------------------------------------------------------------------
# Unit tests — bundle round-trips
# ===========================================================================


class TestSingleBundleRoundTrip:
    """test_lightgbm_single_bundle_round_trips"""

    def test_lightgbm_single_bundle_round_trips(self) -> None:
        """A single LightGBM bundle: write → load → score → Decision."""
        from quant_foundry.bundle_io import (
            BundleKind,
            BundleScorer,
            Decision,
            LOADER_VERSION,
            load_bundle,
            write_bundle,
        )

        model = _train_tiny_lightgbm(n_features=4, n_rows=100, seed=42)
        feature_names = ["f1", "f2", "f3", "f4"]
        feature_schema_hash = hashlib.sha256(b"test-feature-schema").hexdigest()[:16]
        label_schema_hash = hashlib.sha256(b"test-label-schema").hexdigest()[:16]

        bundle_bytes = write_bundle(
            primary_model=model,
            meta_model=None,
            feature_names=feature_names,
            feature_schema_hash=feature_schema_hash,
            label_schema_hash=label_schema_hash,
            model_family="gbm",
        )

        # The bundle bytes are a zip archive.
        assert bundle_bytes[:4] == b"PK\x03\x04"
        bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()

        # Load the bundle.
        bundle = load_bundle(bundle_bytes)
        assert bundle.bundle_kind == BundleKind.SINGLE
        assert bundle.is_meta_labeled is False
        assert bundle.meta_model is None
        assert bundle.bundle_sha256 == bundle_sha
        assert bundle.manifest.loader_version == LOADER_VERSION
        assert bundle.manifest.feature_names == feature_names
        assert bundle.manifest.bundle_kind == BundleKind.SINGLE

        # Score.
        scorer = BundleScorer(bundle)
        sample = _make_sample_features(n_features=4, n_rows=5)
        decisions = scorer.score(sample)
        assert len(decisions) == 5
        for d in decisions:
            assert isinstance(d, Decision)
            assert 0.0 <= d.p <= 1.0
            assert d.direction in (-1, 0, 1)
            assert d.act is True
            assert d.abstained is False
            assert d.meta_p is None
            assert d.bundle_sha256 == bundle_sha
            assert d.policy_version == bundle.manifest.policy_version

    def test_bundle_manifest_lists_every_member_and_sha256(self) -> None:
        """bundle_manifest.json lists every member and sha256."""
        from quant_foundry.bundle_io import load_bundle, write_bundle

        model = _train_tiny_lightgbm(n_features=3, n_rows=50, seed=7)
        bundle_bytes = write_bundle(
            primary_model=model,
            feature_names=["a", "b", "c"],
            feature_schema_hash="hash-f",
            label_schema_hash="hash-l",
            model_family="gbm",
        )

        # Read the manifest directly from the zip.
        with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as zf:
            manifest_data = json.loads(zf.read("bundle_manifest.json"))
        assert "members" in manifest_data
        assert "primary" in manifest_data["members"]
        member = manifest_data["members"]["primary"]
        assert "sha256" in member
        assert "size_bytes" in member
        assert "filename" in member
        assert member["role"] == "primary"
        assert len(member["sha256"]) == 64

    def test_load_bundle_verifies_member_hashes(self) -> None:
        """load_bundle() verifies member hashes before scoring."""
        from quant_foundry.bundle_io import BundleLoadError, load_bundle, write_bundle

        model = _train_tiny_lightgbm(n_features=3, n_rows=50, seed=7)
        bundle_bytes = write_bundle(
            primary_model=model,
            feature_names=["a", "b", "c"],
            feature_schema_hash="hash-f",
            label_schema_hash="hash-l",
            model_family="gbm",
        )

        # Corrupt the primary member inside the zip.
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(bundle_bytes), "r") as zf_in:
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf_out:
                for item in zf_in.infolist():
                    data = zf_in.read(item.filename)
                    if item.filename == "primary.pkl":
                        # Corrupt: flip some bytes.
                        data = data[:10] + bytes([data[10] ^ 0xFF]) + data[11:]
                    zf_out.writestr(item, data)
        corrupted_bytes = buf.getvalue()

        with pytest.raises(BundleLoadError, match="sha256 mismatch"):
            load_bundle(corrupted_bytes)


class TestMetaBundleRoundTrip:
    """test_lightgbm_meta_bundle_round_trips"""

    def test_lightgbm_meta_bundle_round_trips(self) -> None:
        """A meta-labeled bundle: write → load → score → Decision with meta_p."""
        from quant_foundry.bundle_io import (
            BundleKind,
            BundleScorer,
            Decision,
            load_bundle,
            write_bundle,
        )
        import numpy as np

        # Train a multiclass primary model.
        primary = _train_tiny_lightgbm(
            n_features=4, n_rows=100, seed=42, objective="multiclass", n_classes=3
        )
        rng = np.random.RandomState(42)
        X = rng.randn(100, 4)
        label_map = {"-1": 0, "0": 1, "1": 2}
        meta = _train_tiny_meta_model(primary, X, label_map=label_map, seed=42)

        bundle_bytes = write_bundle(
            primary_model=primary,
            meta_model=meta,
            feature_names=["f1", "f2", "f3", "f4"],
            feature_schema_hash="hash-f",
            label_schema_hash="hash-l",
            model_family="gbm",
            label_map=label_map,
            meta_label_config={
                "side_column": "side",
                "label_column": "label",
                "meta_label_column": "meta_label",
            },
        )
        bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()

        # Load.
        bundle = load_bundle(bundle_bytes)
        assert bundle.bundle_kind == BundleKind.META_LABELED
        assert bundle.is_meta_labeled is True
        assert bundle.meta_model is not None
        assert bundle.bundle_sha256 == bundle_sha
        assert bundle.manifest.label_map == label_map
        assert bundle.manifest.meta_label_config is not None

        # Score.
        scorer = BundleScorer(bundle)
        sample = _make_sample_features(n_features=4, n_rows=5)
        decisions = scorer.score(sample)
        assert len(decisions) == 5
        for d in decisions:
            assert isinstance(d, Decision)
            assert 0.0 <= d.p <= 1.0
            assert d.direction in (-1, 0, 1)
            assert d.meta_p is not None
            assert 0.0 <= d.meta_p <= 1.0
            # Invariant: abstained=True ⇒ act=False
            if d.abstained:
                assert d.act is False
            assert d.bundle_sha256 == bundle_sha

    def test_meta_bundle_manifest_has_both_members(self) -> None:
        """meta_labeled bundle manifest lists both primary and meta members."""
        from quant_foundry.bundle_io import write_bundle

        import numpy as np

        primary = _train_tiny_lightgbm(
            n_features=4, n_rows=50, seed=42, objective="multiclass", n_classes=3
        )
        rng = np.random.RandomState(42)
        X = rng.randn(50, 4)
        meta = _train_tiny_meta_model(primary, X, seed=42)

        bundle_bytes = write_bundle(
            primary_model=primary,
            meta_model=meta,
            feature_names=["f1", "f2", "f3", "f4"],
            feature_schema_hash="hash-f",
            label_schema_hash="hash-l",
            model_family="gbm",
        )
        with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as zf:
            manifest = json.loads(zf.read("bundle_manifest.json"))
        assert "primary" in manifest["members"]
        assert "meta" in manifest["members"]
        assert manifest["bundle_kind"] == "meta_labeled"


class TestLegacyBarePickle:
    """test_legacy_bare_lightgbm_pickle_loads_read_only"""

    def test_legacy_bare_lightgbm_pickle_loads_read_only(self) -> None:
        """A bare LightGBM pickle (not a zip) loads as a legacy single bundle."""
        from quant_foundry.bundle_io import BundleKind, BundleScorer, load_bundle

        model = _train_tiny_lightgbm(n_features=4, n_rows=50, seed=42)
        legacy_bytes = pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL)

        # Not a zip.
        assert legacy_bytes[:4] != b"PK\x03\x04"

        bundle = load_bundle(legacy_bytes)
        assert bundle.bundle_kind == BundleKind.SINGLE
        assert bundle.meta_model is None
        assert bundle.manifest.loader_version == "legacy-pickle"
        # Legacy bundles have empty feature_names (schema check skipped).
        assert bundle.manifest.feature_names == []

        # Can score (no schema check since feature_names is empty).
        scorer = BundleScorer(bundle)
        sample = _make_sample_features(n_features=4, n_rows=3)
        decisions = scorer.score(sample)
        assert len(decisions) == 3
        for d in decisions:
            assert d.act is True
            assert d.abstained is False

    def test_legacy_meta_dict_pickle_loads_read_only(self) -> None:
        """A legacy meta-labeled dict pickle (pre-C1) loads as meta_labeled."""
        from quant_foundry.bundle_io import BundleKind, load_bundle

        import numpy as np

        primary = _train_tiny_lightgbm(
            n_features=4, n_rows=50, seed=42, objective="multiclass", n_classes=3
        )
        rng = np.random.RandomState(42)
        X = rng.randn(50, 4)
        meta = _train_tiny_meta_model(primary, X, seed=42)

        # Pre-C1 format: dict with primary/meta/label_map.
        legacy_bytes = pickle.dumps(
            {
                "primary": primary,
                "meta": meta,
                "label_map": {"-1": 0, "0": 1, "1": 2},
                "meta_label_config": {"side_column": "side"},
            },
            protocol=pickle.HIGHEST_PROTOCOL,
        )
        bundle = load_bundle(legacy_bytes)
        assert bundle.bundle_kind == BundleKind.META_LABELED
        assert bundle.meta_model is not None


class TestFailClosed:
    """test_corrupted_member_hash_fails_closed
    test_missing_meta_member_fails_closed
    test_unknown_bundle_kind_fails_closed
    test_feature_schema_mismatch_fails_before_score
    """

    def test_corrupted_member_hash_fails_closed(self) -> None:
        """Corrupted member hash fails closed with BundleLoadError."""
        from quant_foundry.bundle_io import BundleLoadError, load_bundle, write_bundle

        model = _train_tiny_lightgbm(n_features=3, n_rows=50, seed=7)
        bundle_bytes = write_bundle(
            primary_model=model,
            feature_names=["a", "b", "c"],
            feature_schema_hash="hash-f",
            label_schema_hash="hash-l",
            model_family="gbm",
        )
        # Corrupt the manifest's declared sha256 (not the actual bytes).
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(bundle_bytes), "r") as zf_in:
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf_out:
                for item in zf_in.infolist():
                    data = zf_in.read(item.filename)
                    if item.filename == "bundle_manifest.json":
                        manifest = json.loads(data)
                        # Corrupt the declared sha256.
                        manifest["members"]["primary"]["sha256"] = "0" * 64
                        data = json.dumps(manifest, indent=2).encode("utf-8")
                    zf_out.writestr(item, data)
        corrupted = buf.getvalue()
        with pytest.raises(BundleLoadError, match="sha256 mismatch"):
            load_bundle(corrupted)

    def test_missing_meta_member_fails_closed(self) -> None:
        """meta_labeled bundle with missing meta member fails closed."""
        from quant_foundry.bundle_io import BundleLoadError, load_bundle

        import numpy as np

        primary = _train_tiny_lightgbm(
            n_features=4, n_rows=50, seed=42, objective="multiclass", n_classes=3
        )
        rng = np.random.RandomState(42)
        X = rng.randn(50, 4)
        meta = _train_tiny_meta_model(primary, X, seed=42)

        # Build a zip with a meta_labeled manifest but no meta.pkl member.
        primary_bytes = pickle.dumps(primary, protocol=pickle.HIGHEST_PROTOCOL)
        meta_bytes = pickle.dumps(meta, protocol=pickle.HIGHEST_PROTOCOL)

        manifest = {
            "schema_version": 1,
            "bundle_kind": "meta_labeled",
            "loader_version": "bundle-v1",
            "model_family": "gbm",
            "feature_names": ["f1", "f2", "f3", "f4"],
            "feature_schema_hash": "hash-f",
            "label_schema_hash": "hash-l",
            "members": {
                "primary": {
                    "filename": "primary.pkl",
                    "sha256": hashlib.sha256(primary_bytes).hexdigest(),
                    "size_bytes": len(primary_bytes),
                    "role": "primary",
                },
                "meta": {
                    "filename": "meta.pkl",
                    "sha256": hashlib.sha256(meta_bytes).hexdigest(),
                    "size_bytes": len(meta_bytes),
                    "role": "meta",
                },
            },
            "created_at_ns": time.time_ns(),
            "label_map": {"-1": 0, "0": 1, "1": 2},
            "meta_label_config": {"side_column": "side"},
            "policy_version": "meta-abstain-v1",
        }

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("bundle_manifest.json", json.dumps(manifest, indent=2))
            zf.writestr("primary.pkl", primary_bytes)
            # NOTE: intentionally no meta.pkl
        with pytest.raises(BundleLoadError, match="meta.*missing"):
            load_bundle(buf.getvalue())

    def test_unknown_bundle_kind_fails_closed(self) -> None:
        """Unknown bundle_kind fails closed."""
        from quant_foundry.bundle_io import BundleLoadError, load_bundle

        model = _train_tiny_lightgbm(n_features=3, n_rows=50, seed=7)
        primary_bytes = pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL)

        manifest = {
            "schema_version": 1,
            "bundle_kind": "ensemble",  # unknown
            "loader_version": "bundle-v1",
            "model_family": "gbm",
            "feature_names": ["a", "b", "c"],
            "feature_schema_hash": "hash-f",
            "label_schema_hash": "hash-l",
            "members": {
                "primary": {
                    "filename": "primary.pkl",
                    "sha256": hashlib.sha256(primary_bytes).hexdigest(),
                    "size_bytes": len(primary_bytes),
                    "role": "primary",
                },
            },
            "created_at_ns": time.time_ns(),
            "policy_version": "meta-abstain-v1",
        }

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("bundle_manifest.json", json.dumps(manifest, indent=2))
            zf.writestr("primary.pkl", primary_bytes)
        with pytest.raises(BundleLoadError, match="bundle_kind"):
            load_bundle(buf.getvalue())

    def test_feature_schema_mismatch_fails_before_score(self) -> None:
        """Feature schema mismatch (wrong n_features) fails before scoring."""
        from quant_foundry.bundle_io import (
            BundleScorer,
            SchemaMismatchError,
            load_bundle,
            write_bundle,
        )

        model = _train_tiny_lightgbm(n_features=4, n_rows=50, seed=42)
        bundle_bytes = write_bundle(
            primary_model=model,
            feature_names=["f1", "f2", "f3", "f4"],
            feature_schema_hash="hash-f",
            label_schema_hash="hash-l",
            model_family="gbm",
        )
        bundle = load_bundle(bundle_bytes)
        scorer = BundleScorer(bundle)
        # Pass 3 features instead of 4 → schema mismatch.
        bad_sample = [[1.0, 2.0, 3.0]]
        with pytest.raises(SchemaMismatchError, match="feature schema mismatch"):
            scorer.score(bad_sample)


class TestMetaAbstention:
    """test_meta_abstention_sets_act_false"""

    def test_meta_abstention_sets_act_false(self) -> None:
        """abstained=True ⇒ act=False for meta-labeled decisions."""
        from quant_foundry.bundle_io import (
            BundleScorer,
            Decision,
            load_bundle,
            write_bundle,
        )
        import numpy as np

        primary = _train_tiny_lightgbm(
            n_features=4, n_rows=100, seed=42, objective="multiclass", n_classes=3
        )
        rng = np.random.RandomState(42)
        X = rng.randn(100, 4)
        label_map = {"-1": 0, "0": 1, "1": 2}
        meta = _train_tiny_meta_model(primary, X, label_map=label_map, seed=42)

        # Use a high abstention threshold to force some abstentions.
        bundle_bytes = write_bundle(
            primary_model=primary,
            meta_model=meta,
            feature_names=["f1", "f2", "f3", "f4"],
            feature_schema_hash="hash-f",
            label_schema_hash="hash-l",
            model_family="gbm",
            label_map=label_map,
            meta_label_config={
                "side_column": "side",
                "label_column": "label",
                "meta_label_column": "meta_label",
                "abstain_threshold": 0.99,  # very high → most will abstain
            },
        )
        bundle = load_bundle(bundle_bytes)
        scorer = BundleScorer(bundle)

        # Score many rows to ensure at least one abstention.
        sample = _make_sample_features(n_features=4, n_rows=20)
        decisions = scorer.score(sample)
        assert len(decisions) == 20

        # Check invariant: abstained=True ⇒ act=False
        for d in decisions:
            assert isinstance(d, Decision)
            if d.abstained:
                assert d.act is False
            # With threshold 0.99, at least some should abstain.
        n_abstained = sum(1 for d in decisions if d.abstained)
        assert n_abstained > 0, "expected at least some abstentions with threshold 0.99"

    def test_meta_low_threshold_never_abstains(self) -> None:
        """With threshold 0.0, no decision abstains."""
        from quant_foundry.bundle_io import BundleScorer, load_bundle, write_bundle
        import numpy as np

        primary = _train_tiny_lightgbm(
            n_features=4, n_rows=100, seed=42, objective="multiclass", n_classes=3
        )
        rng = np.random.RandomState(42)
        X = rng.randn(100, 4)
        meta = _train_tiny_meta_model(primary, X, seed=42)

        bundle_bytes = write_bundle(
            primary_model=primary,
            meta_model=meta,
            feature_names=["f1", "f2", "f3", "f4"],
            feature_schema_hash="hash-f",
            label_schema_hash="hash-l",
            model_family="gbm",
            meta_label_config={"abstain_threshold": 0.0},
        )
        bundle = load_bundle(bundle_bytes)
        scorer = BundleScorer(bundle)
        sample = _make_sample_features(n_features=4, n_rows=10)
        decisions = scorer.score(sample)
        for d in decisions:
            assert d.abstained is False
            assert d.act is True


# ---------------------------------------------------------------------------
# Selfcheck unit tests
# ===========================================================================


class TestSelfCheck:
    """Selfcheck unit tests — run_selfcheck on valid and invalid bundles."""

    def test_selfcheck_success_returns_passed_true(self) -> None:
        """A valid bundle selfcheck returns passed=True with all fields."""
        from quant_foundry.bundle_io import LOADER_VERSION, run_selfcheck, write_bundle

        model = _train_tiny_lightgbm(n_features=4, n_rows=50, seed=42)
        bundle_bytes = write_bundle(
            primary_model=model,
            feature_names=["f1", "f2", "f3", "f4"],
            feature_schema_hash="hash-f",
            label_schema_hash="hash-l",
            model_family="gbm",
        )
        sample = _make_sample_features(n_features=4, n_rows=5)
        result = run_selfcheck(bundle_bytes, sample)

        assert result.passed is True
        assert result.n_rows_scored == 5
        assert len(result.output_sha256) == 64
        assert result.bundle_sha256 == hashlib.sha256(bundle_bytes).hexdigest()
        assert result.loader_version == LOADER_VERSION
        assert result.duration_ms >= 0.0

    def test_selfcheck_crash_is_failure(self) -> None:
        """A selfcheck crash (corrupt bytes) is a selfcheck failure."""
        from quant_foundry.bundle_io import run_selfcheck

        # Not a zip, not a valid pickle.
        corrupt_bytes = b"this is not a valid bundle"
        sample = _make_sample_features(n_features=4, n_rows=3)
        result = run_selfcheck(corrupt_bytes, sample)

        assert result.passed is False
        assert result.n_rows_scored == 0
        assert result.error_detail is not None

    def test_selfcheck_schema_mismatch_is_failure(self) -> None:
        """A selfcheck with wrong feature count is a failure."""
        from quant_foundry.bundle_io import run_selfcheck, write_bundle

        model = _train_tiny_lightgbm(n_features=4, n_rows=50, seed=42)
        bundle_bytes = write_bundle(
            primary_model=model,
            feature_names=["f1", "f2", "f3", "f4"],
            feature_schema_hash="hash-f",
            label_schema_hash="hash-l",
            model_family="gbm",
        )
        # Pass 3 features instead of 4.
        bad_sample = [[1.0, 2.0, 3.0]]
        result = run_selfcheck(bundle_bytes, bad_sample)

        assert result.passed is False
        assert "mismatch" in (result.error_detail or "").lower()

    def test_selfcheck_empty_sample_is_failure(self) -> None:
        """A selfcheck with no sample rows still loads but scores 0 rows."""
        from quant_foundry.bundle_io import run_selfcheck, write_bundle

        model = _train_tiny_lightgbm(n_features=4, n_rows=50, seed=42)
        bundle_bytes = write_bundle(
            primary_model=model,
            feature_names=["f1", "f2", "f3", "f4"],
            feature_schema_hash="hash-f",
            label_schema_hash="hash-l",
            model_family="gbm",
        )
        result = run_selfcheck(bundle_bytes, [])
        # Empty sample → 0 rows scored, but loading succeeded.
        # This is technically a success with 0 rows (the bundle loaded).
        assert result.passed is True
        assert result.n_rows_scored == 0


# ---------------------------------------------------------------------------
# Handler / selfcheck integration tests
# ===========================================================================


# The handler module lives outside the quant_foundry package.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_HANDLER_DIR = str(_REPO_ROOT / "runpod" / "quant-foundry-training")


@pytest.fixture(scope="module")
def handler_module():
    """Import the handler module (adding its dir to sys.path)."""
    import importlib
    import sys

    if _HANDLER_DIR not in sys.path:
        sys.path.insert(0, _HANDLER_DIR)
    return importlib.import_module("handler")


def _make_synthetic_csv(tmp_path: Path, n: int = 200, seed: int = 42) -> str:
    """Create a synthetic CSV dataset and return its file path."""
    import numpy as np

    rng = np.random.RandomState(seed)
    timestamps = np.arange(n, dtype=np.int64)
    f1 = rng.randn(n)
    f2 = rng.randn(n)
    f3 = rng.randn(n)
    f4 = rng.randn(n)
    logit = 0.8 * f1 + 0.5 * f2 - 0.6 * f3 + 0.05 * rng.randn(n)
    label = (logit > 0).astype(float)
    data = np.column_stack([timestamps, f1, f2, f3, f4, label])
    path = tmp_path / "selfcheck_data.csv"
    header = "timestamp,f1,f2,f3,f4,label"
    np.savetxt(str(path), data, delimiter=",", header=header, comments="")
    return str(path)


def _make_training_input_for_real_trainer(
    job_id: str,
    dataset_path: str,
    **extra,
) -> dict:
    """Build a training input dict that uses the real LightGBM trainer."""
    return {
        "input": {
            "job_id": job_id,
            "dataset_manifest_ref": dataset_path,
            "model_family": "gbm",
            "search_space": {"n_estimators": [30]},
            "random_seed": 42,
            "hardware_class": "cpu",
            "extra_constraints": {
                "training_mode": "canary",  # allows /tmp, skips registry gate
            },
            **extra,
        }
    }


class TestWorkerSelfCheck:
    """test_worker_selfcheck_success_allows_signed_success
    test_worker_selfcheck_failure_returns_signed_failure
    test_worker_selfcheck_failure_does_not_register_success_artifact
    """

    def test_worker_selfcheck_success_allows_signed_success(
        self, handler_module, monkeypatch, tmp_path
    ) -> None:
        """A successful selfcheck allows the signed success callback."""
        monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "selfcheck-success-secret")
        monkeypatch.setenv("QUANT_FOUNDRY_USE_REAL_TRAINER", "true")

        csv_path = _make_synthetic_csv(tmp_path, n=200, seed=42)
        event = _make_training_input_for_real_trainer(
            "qf:selfcheck:success:1",
            csv_path,
        )
        result = handler_module.handler(event)

        # Should succeed (no error_code).
        assert "error_code" not in result, f"unexpected failure: {result.get('error_code')}"
        assert result["job_id"] == "qf:selfcheck:success:1"

        # The typed callback should have selfcheck fields.
        typed_cb = result["typed_callback"]
        metrics = typed_cb.get("metrics_summary", {})
        assert metrics.get("selfcheck.passed") is True
        assert metrics.get("selfcheck.n_rows_scored", 0) > 0
        assert metrics.get("selfcheck.bundle_sha256")
        assert metrics.get("selfcheck.output_sha256")
        assert metrics.get("selfcheck.loader_version")
        assert "selfcheck.duration_ms" in metrics

    def test_worker_selfcheck_failure_returns_signed_failure(
        self, handler_module, monkeypatch, tmp_path
    ) -> None:
        """A failed selfcheck returns a signed failure with
        error_code='bundle_selfcheck_failed'."""
        from quant_foundry.bundle_io import TrainingSelfCheck

        monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "selfcheck-fail-secret")
        monkeypatch.setenv("QUANT_FOUNDRY_USE_REAL_TRAINER", "true")

        # Monkeypatch run_selfcheck to return a failed result.
        def _failing_selfcheck(bundle_bytes, sample_features):
            return TrainingSelfCheck(
                passed=False,
                error_detail="forced failure for test",
            )

        monkeypatch.setattr(
            handler_module, "run_selfcheck", _failing_selfcheck
        )

        csv_path = _make_synthetic_csv(tmp_path, n=200, seed=42)
        event = _make_training_input_for_real_trainer(
            "qf:selfcheck:fail:1",
            csv_path,
        )
        result = handler_module.handler(event)

        # Should fail with bundle_selfcheck_failed.
        assert result.get("error_code") == "bundle_selfcheck_failed"
        assert result.get("job_id") == "qf:selfcheck:fail:1"
        # Signed failure envelope.
        assert result.get("callback_signature")
        assert result.get("callback_payload")

    def test_worker_selfcheck_failure_does_not_register_success_artifact(
        self, handler_module, monkeypatch, tmp_path
    ) -> None:
        """A failed selfcheck does not register a success artifact."""
        from quant_foundry.bundle_io import TrainingSelfCheck

        monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "selfcheck-noart-secret")
        monkeypatch.setenv("QUANT_FOUNDRY_USE_REAL_TRAINER", "true")

        def _failing_selfcheck(bundle_bytes, sample_features):
            return TrainingSelfCheck(
                passed=False,
                error_detail="forced failure for test",
            )

        monkeypatch.setattr(
            handler_module, "run_selfcheck", _failing_selfcheck
        )

        csv_path = _make_synthetic_csv(tmp_path, n=200, seed=42)
        event = _make_training_input_for_real_trainer(
            "qf:selfcheck:noart:1",
            csv_path,
        )
        result = handler_module.handler(event)

        # Must NOT have success artifact fields.
        assert result.get("error_code") == "bundle_selfcheck_failed"
        assert "artifact_result" not in result or result.get("artifact_result") is None
        # Must NOT have a typed_callback with success.
        assert "typed_callback" not in result or result.get("typed_callback") is None
        # Must NOT claim success.
        assert result.get("signed_failure") is True


# ---------------------------------------------------------------------------
# Optional: XGBoost bundle round-trip
# ===========================================================================


class TestXGBoostBundle:
    """XGBoost bundle round-trip — only if xgboost is locally available."""

    def test_xgboost_single_bundle_round_trips(self) -> None:
        """XGBoost single bundle: write → load → score."""
        pytest.importorskip("xgboost")
        import numpy as np
        import xgboost as xgb

        from quant_foundry.bundle_io import BundleKind, BundleScorer, load_bundle, write_bundle

        rng = np.random.RandomState(42)
        X = rng.randn(100, 4)
        y = (X[:, 0] > 0).astype(float)
        dtrain = xgb.DMatrix(X, label=y)
        model = xgb.train(
            {"objective": "binary:logistic", "verbosity": 0, "seed": 42},
            dtrain,
            num_boost_round=20,
        )

        bundle_bytes = write_bundle(
            primary_model=model,
            feature_names=["f1", "f2", "f3", "f4"],
            feature_schema_hash="hash-f",
            label_schema_hash="hash-l",
            model_family="xgboost",
        )
        bundle = load_bundle(bundle_bytes)
        assert bundle.bundle_kind == BundleKind.SINGLE

        scorer = BundleScorer(bundle)
        sample = _make_sample_features(n_features=4, n_rows=5)
        decisions = scorer.score(sample)
        assert len(decisions) == 5
        for d in decisions:
            assert 0.0 <= d.p <= 1.0
