"""
Tests for quant_foundry.modules.versioning — dataset versioning + lineage.

Tests verify:
- DatasetVersion creation + field access.
- DatasetVersionRegistry: register, retrieve lineage, latest, next number.
- Lineage chain: parent links across 3 versions.
- Diff between versions with changed module configs.
- Module config hash: deterministic, sensitive to changes.
- list_datasets + compare_datasets.
- DatasetVersion is frozen (immutable).
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _make_version(
    dataset_id: str = "ds-a",
    version_number: int = 1,
    *,
    parent_version_id: str | None = None,
    module_config: dict[str, str] | None = None,
    row_count: int = 100,
    build_mode: str = "full",
    content_hash: str = "hash-1",
    parquet_path: str = "/tmp/ds-a.parquet",
    manifest_path: str = "/tmp/ds-a.manifest.json",
) -> object:
    from quant_foundry.modules.versioning import DatasetVersion, make_version_id

    return DatasetVersion(
        version_id=make_version_id(dataset_id, version_number),
        dataset_id=dataset_id,
        version_number=version_number,
        created_at_ns=1_700_000_000_000_000_000 + version_number,
        parquet_path=parquet_path,
        manifest_path=manifest_path,
        row_count=row_count,
        module_config=module_config
        or {
            "universe": "universe:sp500:1.0.0",
            "source": "source:mock:1.0.0",
            "sentiment": "sentiment:naive-wordlist:1.0.0",
            "features": "feature:per-event-type:1.0.0",
            "label": "label:abnormal-return-v1:1.0.0",
            "price_join": "price_join:mock:1.0.0",
        },
        module_config_hash="cfg-hash-" + str(version_number),
        parent_version_id=parent_version_id,
        build_mode=build_mode,
        content_hash=content_hash,
    )


# --------------------------------------------------------------------------- #
# DatasetVersion                                                               #
# --------------------------------------------------------------------------- #


def test_dataset_version_creation() -> None:
    """Create a DatasetVersion, verify all fields."""
    from quant_foundry.modules.versioning import DatasetVersion, make_version_id

    cfg = {
        "universe": "universe:sp500:1.0.0",
        "sentiment": "sentiment:finbert:1.0.0",
    }
    v = DatasetVersion(
        version_id=make_version_id("ds-x", 2),
        dataset_id="ds-x",
        version_number=2,
        created_at_ns=1_700_000_000_000_000_000,
        parquet_path="/tmp/x.parquet",
        manifest_path="/tmp/x.manifest.json",
        row_count=42,
        module_config=cfg,
        module_config_hash="abc123",
        parent_version_id="ds-x:v001",
        build_mode="incremental",
        content_hash="deadbeef",
    )

    assert v.version_id == "ds-x:v002"
    assert v.dataset_id == "ds-x"
    assert v.version_number == 2
    assert v.created_at_ns == 1_700_000_000_000_000_000
    assert v.parquet_path == "/tmp/x.parquet"
    assert v.manifest_path == "/tmp/x.manifest.json"
    assert v.row_count == 42
    assert v.module_config == cfg
    assert v.module_config_hash == "abc123"
    assert v.parent_version_id == "ds-x:v001"
    assert v.build_mode == "incremental"
    assert v.content_hash == "deadbeef"


def test_versioning_frozen() -> None:
    """DatasetVersion is frozen (can't modify fields)."""
    v = _make_version()
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.row_count = 999  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.build_mode = "incremental"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# DatasetVersionRegistry                                                       #
# --------------------------------------------------------------------------- #


def test_dataset_version_registry(tmp_path: pathlib.Path) -> None:
    """Register versions, retrieve lineage."""
    from quant_foundry.modules.versioning import DatasetVersionRegistry

    registry = DatasetVersionRegistry(tmp_path / "versions")
    v1 = _make_version(version_number=1)
    registry.register_version(v1)

    lineage = registry.get_lineage("ds-a")
    assert lineage.dataset_id == "ds-a"
    assert len(lineage.versions) == 1
    assert lineage.versions[0].version_number == 1

    # Persistence: a new registry instance reads the same file.
    registry2 = DatasetVersionRegistry(tmp_path / "versions")
    lineage2 = registry2.get_lineage("ds-a")
    assert len(lineage2.versions) == 1
    assert lineage2.versions[0].version_id == v1.version_id


def test_lineage_chain(tmp_path: pathlib.Path) -> None:
    """Register 3 versions, verify chain (v1 parent=None, v2 parent=v1, v3 parent=v2)."""
    from quant_foundry.modules.versioning import DatasetVersionRegistry, make_version_id

    registry = DatasetVersionRegistry(tmp_path / "versions")
    v1 = _make_version(version_number=1, parent_version_id=None)
    registry.register_version(v1)

    v2 = _make_version(
        version_number=2,
        parent_version_id=make_version_id("ds-a", 1),
        content_hash="hash-2",
    )
    registry.register_version(v2)

    v3 = _make_version(
        version_number=3,
        parent_version_id=make_version_id("ds-a", 2),
        content_hash="hash-3",
    )
    registry.register_version(v3)

    lineage = registry.get_lineage("ds-a")
    assert len(lineage.versions) == 3
    assert lineage.versions[0].parent_version_id is None
    assert lineage.versions[1].parent_version_id == "ds-a:v001"
    assert lineage.versions[2].parent_version_id == "ds-a:v002"


def test_latest_version(tmp_path: pathlib.Path) -> None:
    """Register 3 versions, verify latest is v3."""
    from quant_foundry.modules.versioning import DatasetVersionRegistry

    registry = DatasetVersionRegistry(tmp_path / "versions")
    for n in (1, 2, 3):
        registry.register_version(_make_version(version_number=n))

    latest = registry.latest_version("ds-a")
    assert latest is not None
    assert latest.version_number == 3

    # Empty dataset returns None.
    assert registry.latest_version("nonexistent") is None


def test_next_version_number(tmp_path: pathlib.Path) -> None:
    """Empty registry returns 1, with 2 versions returns 3."""
    from quant_foundry.modules.versioning import DatasetVersionRegistry

    registry = DatasetVersionRegistry(tmp_path / "versions")
    assert registry.next_version_number("ds-a") == 1

    registry.register_version(_make_version(version_number=1))
    registry.register_version(_make_version(version_number=2))
    assert registry.next_version_number("ds-a") == 3


# --------------------------------------------------------------------------- #
# Diff                                                                         #
# --------------------------------------------------------------------------- #


def test_diff_between_versions(tmp_path: pathlib.Path) -> None:
    """Register 2 versions with different module configs, verify diff shows module changes."""
    from quant_foundry.modules.versioning import DatasetVersionRegistry

    registry = DatasetVersionRegistry(tmp_path / "versions")
    cfg_v1 = {
        "universe": "universe:sp500:1.0.0",
        "source": "source:mock:1.0.0",
        "sentiment": "sentiment:naive-wordlist:1.0.0",
        "features": "feature:per-event-type:1.0.0",
        "label": "label:abnormal-return-v1:1.0.0",
        "price_join": "price_join:mock:1.0.0",
    }
    cfg_v2 = {
        "universe": "universe:sp500:1.0.0",
        "source": "source:mock:1.0.0",
        "sentiment": "sentiment:finbert:1.0.0",  # changed
        "features": "feature:per-event-type:1.0.0",
        "label": "label:abnormal-return-v1:1.0.0",
        "price_join": "price_join:mock:1.0.0",
    }

    registry.register_version(
        _make_version(version_number=1, module_config=cfg_v1, content_hash="h1", row_count=100),
    )
    registry.register_version(
        _make_version(version_number=2, module_config=cfg_v2, content_hash="h2", row_count=120),
    )

    lineage = registry.get_lineage("ds-a")
    diff = lineage.diff(1, 2)
    assert "sentiment" in diff["module_changes"]
    old, new = diff["module_changes"]["sentiment"]
    assert old == "sentiment:naive-wordlist:1.0.0"
    assert new == "sentiment:finbert:1.0.0"
    assert diff["row_count_delta"] == 20
    assert diff["content_changed"] is True


# --------------------------------------------------------------------------- #
# Module config hash                                                           #
# --------------------------------------------------------------------------- #


def test_module_config_hash() -> None:
    """Same config produces same hash, different config produces different hash."""
    from quant_foundry.modules.versioning import compute_module_config_hash

    h1 = compute_module_config_hash(
        universe="universe:sp500:1.0.0",
        source="source:mock:1.0.0",
        sentiment="sentiment:naive-wordlist:1.0.0",
        features=["feature:per-event-type:1.0.0"],
        label="label:abnormal-return-v1:1.0.0",
        price_join="price_join:mock:1.0.0",
    )
    h2 = compute_module_config_hash(
        universe="universe:sp500:1.0.0",
        source="source:mock:1.0.0",
        sentiment="sentiment:naive-wordlist:1.0.0",
        features=["feature:per-event-type:1.0.0"],
        label="label:abnormal-return-v1:1.0.0",
        price_join="price_join:mock:1.0.0",
    )
    assert h1 == h2
    assert len(h1) == 64  # SHA256 hex

    h3 = compute_module_config_hash(
        universe="universe:sp500:1.0.0",
        source="source:mock:1.0.0",
        sentiment="sentiment:finbert:1.0.0",  # changed
        features=["feature:per-event-type:1.0.0"],
        label="label:abnormal-return-v1:1.0.0",
        price_join="price_join:mock:1.0.0",
    )
    assert h1 != h3

    # Feature order is insensitive.
    h4 = compute_module_config_hash(
        universe="universe:sp500:1.0.0",
        source="source:mock:1.0.0",
        sentiment="sentiment:naive-wordlist:1.0.0",
        features=["feature:per-event-type:1.0.0", "feature:per-year:1.0.0"],
        label="label:abnormal-return-v1:1.0.0",
        price_join="price_join:mock:1.0.0",
    )
    h5 = compute_module_config_hash(
        universe="universe:sp500:1.0.0",
        source="source:mock:1.0.0",
        sentiment="sentiment:naive-wordlist:1.0.0",
        features=["feature:per-year:1.0.0", "feature:per-event-type:1.0.0"],
        label="label:abnormal-return-v1:1.0.0",
        price_join="price_join:mock:1.0.0",
    )
    assert h4 == h5


# --------------------------------------------------------------------------- #
# list_datasets + compare_datasets                                             #
# --------------------------------------------------------------------------- #


def test_list_datasets(tmp_path: pathlib.Path) -> None:
    """Register versions for 2 different datasets, verify both listed."""
    from quant_foundry.modules.versioning import DatasetVersionRegistry

    registry = DatasetVersionRegistry(tmp_path / "versions")
    registry.register_version(_make_version(dataset_id="ds-a", version_number=1))
    registry.register_version(_make_version(dataset_id="ds-b", version_number=1))

    datasets = registry.list_datasets()
    assert "ds-a" in datasets
    assert "ds-b" in datasets
    assert len(datasets) == 2


def test_compare_datasets(tmp_path: pathlib.Path) -> None:
    """Compare latest versions of two datasets."""
    from quant_foundry.modules.versioning import DatasetVersionRegistry

    registry = DatasetVersionRegistry(tmp_path / "versions")
    cfg_a = {
        "universe": "universe:sp500:1.0.0",
        "source": "source:mock:1.0.0",
        "sentiment": "sentiment:naive-wordlist:1.0.0",
        "features": "feature:per-event-type:1.0.0",
        "label": "label:abnormal-return-v1:1.0.0",
        "price_join": "price_join:mock:1.0.0",
    }
    cfg_b = {
        "universe": "universe:sp500:1.0.0",
        "source": "source:mock:1.0.0",
        "sentiment": "sentiment:finbert:1.0.0",  # differs
        "features": "feature:per-event-type:1.0.0",
        "label": "label:abnormal-return-v1:1.0.0",
        "price_join": "price_join:mock:1.0.0",
    }
    registry.register_version(
        _make_version(
            dataset_id="ds-a",
            version_number=1,
            module_config=cfg_a,
            row_count=100,
            content_hash="ha",
        ),
    )
    registry.register_version(
        _make_version(
            dataset_id="ds-b",
            version_number=1,
            module_config=cfg_b,
            row_count=150,
            content_hash="hb",
        ),
    )

    cmp = registry.compare_datasets("ds-a", "ds-b")
    assert cmp["dataset_a"] == "ds-a"
    assert cmp["dataset_b"] == "ds-b"
    assert cmp["version_a"] == 1
    assert cmp["version_b"] == 1
    assert "sentiment" in cmp["module_changes"]
    assert cmp["row_count_delta"] == 50
    assert cmp["content_changed"] is True

    # Comparing a dataset with no versions raises.
    with pytest.raises(ValueError):
        registry.compare_datasets("ds-a", "nonexistent")
