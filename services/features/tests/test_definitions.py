"""Tests for ``features.definitions`` (Tier 2.6).

Tests verify:
- FeatureDefinition: frozen, extra='forbid', field validation.
- FeatureSetVersion: hash computation, create() factory, feature_names.
- compute_feature_set_hash: deterministic, order-independent.
- FeatureRegistry: register, get, list_versions, verify, hash mismatch.
- Edge cases: empty definitions, duplicate names, missing versions.
"""

from __future__ import annotations

import time

import pytest

from features.definitions import (
    FeatureDefinition,
    FeatureRegistry,
    FeatureSetVersion,
    RegistryError,
    compute_feature_set_hash,
)


def _make_def(
    name: str = "ret_log_1",
    transform: str = "price",
    version: str = "1.0.0",
    **params: object,
) -> FeatureDefinition:
    return FeatureDefinition(
        feature_name=name,
        transform_type=transform,
        parameters=dict(params),
        version=version,
    )


def _make_set(
    defs: tuple[FeatureDefinition, ...] | None = None,
    set_id: str = "price-vol-v1",
    version: str = "1.0.0",
) -> FeatureSetVersion:
    if defs is None:
        defs = (
            _make_def("ret_log_1", "price"),
            _make_def("mom_20", "price", window=20),
            _make_def("vol_rs_60", "volatility", window=60),
        )
    return FeatureSetVersion.create(
        feature_set_id=set_id,
        version=version,
        feature_definitions=defs,
        created_at_ns=time.time_ns(),
    )


class TestFeatureDefinition:
    def test_basic_creation(self) -> None:
        d = _make_def("ret_log_1", "price")
        assert d.feature_name == "ret_log_1"
        assert d.transform_type == "price"
        assert d.version == "1.0.0"

    def test_frozen(self) -> None:
        d = _make_def()
        with pytest.raises(Exception):
            d.feature_name = "hack"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            FeatureDefinition(  # type: ignore[call-arg]
                feature_name="x", transform_type="price",
                unknown_field=1,
            )

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(Exception):
            _make_def("")

    def test_empty_transform_rejected(self) -> None:
        with pytest.raises(Exception):
            _make_def("ret_log_1", "")

    def test_empty_version_rejected(self) -> None:
        with pytest.raises(Exception):
            _make_def(version="")

    def test_parameters_stored(self) -> None:
        d = _make_def("mom_20", "price", window=20, lookback=20)
        assert d.parameters == {"window": 20, "lookback": 20}


class TestComputeFeatureSetHash:
    def test_deterministic(self) -> None:
        """Same definitions → same hash."""
        defs = (_make_def("a"), _make_def("b"))
        h1 = compute_feature_set_hash(defs)
        h2 = compute_feature_set_hash(defs)
        assert h1 == h2

    def test_order_independent(self) -> None:
        """Different order → same hash (sorted by name)."""
        defs1 = (_make_def("a"), _make_def("b"))
        defs2 = (_make_def("b"), _make_def("a"))
        assert compute_feature_set_hash(defs1) == compute_feature_set_hash(defs2)

    def test_different_defs_different_hash(self) -> None:
        """Different definitions → different hash."""
        defs1 = (_make_def("a"),)
        defs2 = (_make_def("b"),)
        assert compute_feature_set_hash(defs1) != compute_feature_set_hash(defs2)

    def test_different_params_different_hash(self) -> None:
        """Same name, different params → different hash."""
        defs1 = (_make_def("mom_20", window=20),)
        defs2 = (_make_def("mom_20", window=60),)
        assert compute_feature_set_hash(defs1) != compute_feature_set_hash(defs2)

    def test_different_version_different_hash(self) -> None:
        """Same name, different version → different hash."""
        defs1 = (_make_def("ret_log_1", version="1.0.0"),)
        defs2 = (_make_def("ret_log_1", version="2.0.0"),)
        assert compute_feature_set_hash(defs1) != compute_feature_set_hash(defs2)

    def test_hash_is_sha256(self) -> None:
        """Hash is a 64-character hex string."""
        h = compute_feature_set_hash((_make_def(),))
        assert len(h) == 64
        int(h, 16)  # valid hex


class TestFeatureSetVersion:
    def test_create_computes_hash(self) -> None:
        fsv = _make_set()
        assert len(fsv.feature_set_hash) == 64

    def test_feature_names_sorted(self) -> None:
        fsv = _make_set()
        assert list(fsv.feature_names) == sorted(fsv.feature_names)

    def test_frozen(self) -> None:
        fsv = _make_set()
        with pytest.raises(Exception):
            fsv.version = "hack"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            FeatureSetVersion(  # type: ignore[call-arg]
                feature_set_id="x",
                version="1.0.0",
                feature_definitions=(_make_def(),),
                feature_set_hash="a" * 64,
                created_at_ns=0,
                unknown=1,
            )

    def test_empty_definitions_rejected(self) -> None:
        with pytest.raises(Exception):
            FeatureSetVersion.create(
                feature_set_id="x",
                version="1.0.0",
                feature_definitions=(),
                created_at_ns=0,
            )

    def test_hash_matches_compute(self) -> None:
        defs = (_make_def("a"), _make_def("b"))
        fsv = FeatureSetVersion.create(
            feature_set_id="test",
            version="1.0.0",
            feature_definitions=defs,
            created_at_ns=0,
        )
        assert fsv.feature_set_hash == compute_feature_set_hash(defs)

    def test_invalid_hash_shape_rejected(self) -> None:
        with pytest.raises(Exception):
            FeatureSetVersion(
                feature_set_id="x",
                version="1.0.0",
                feature_definitions=(_make_def(),),
                feature_set_hash="short",
                created_at_ns=0,
            )


class TestFeatureRegistry:
    def test_register_and_get(self) -> None:
        reg = FeatureRegistry()
        fsv = _make_set()
        reg.register(fsv)
        assert reg.get("price-vol-v1", "1.0.0") == fsv

    def test_register_idempotent(self) -> None:
        """Re-registering the same version is a no-op."""
        reg = FeatureRegistry()
        fsv = _make_set()
        reg.register(fsv)
        reg.register(fsv)  # no error
        assert len(reg) == 1

    def test_register_hash_mismatch_raises(self) -> None:
        """Different hash for same (id, version) raises."""
        reg = FeatureRegistry()
        fsv1 = _make_set(version="1.0.0")
        reg.register(fsv1)
        # Different definitions → different hash
        fsv2 = _make_set(
            defs=(_make_def("different"),),
            version="1.0.0",
        )
        with pytest.raises(RegistryError, match="different hash"):
            reg.register(fsv2)

    def test_get_not_found_raises(self) -> None:
        reg = FeatureRegistry()
        with pytest.raises(RegistryError, match="not found"):
            reg.get("nonexistent", "1.0.0")

    def test_list_versions(self) -> None:
        reg = FeatureRegistry()
        reg.register(_make_set(version="1.0.0"))
        reg.register(_make_set(version="1.1.0"))
        reg.register(_make_set(version="2.0.0"))
        versions = reg.list_versions("price-vol-v1")
        assert versions == ["1.0.0", "1.1.0", "2.0.0"]

    def test_list_versions_empty(self) -> None:
        reg = FeatureRegistry()
        assert reg.list_versions("nonexistent") == []

    def test_verify_true(self) -> None:
        reg = FeatureRegistry()
        fsv = _make_set()
        reg.register(fsv)
        assert reg.verify("price-vol-v1", "1.0.0") is True

    def test_verify_false(self) -> None:
        reg = FeatureRegistry()
        assert reg.verify("nonexistent", "1.0.0") is False

    def test_len(self) -> None:
        reg = FeatureRegistry()
        assert len(reg) == 0
        reg.register(_make_set(version="1.0.0"))
        assert len(reg) == 1
        reg.register(_make_set(version="2.0.0"))
        assert len(reg) == 2

    def test_different_set_ids(self) -> None:
        """Different feature_set_ids can have same version."""
        reg = FeatureRegistry()
        reg.register(_make_set(set_id="set-a", version="1.0.0"))
        reg.register(_make_set(set_id="set-b", version="1.0.0"))
        assert len(reg) == 2
        assert reg.verify("set-a", "1.0.0")
        assert reg.verify("set-b", "1.0.0")
