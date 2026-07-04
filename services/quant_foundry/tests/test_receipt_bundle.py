"""TDD tests for quant_foundry.receipt_bundle (Phase 6 / T-TV.3).

Acceptance:
- Store receipt bundles under reports/runpod-training/<job-id>/.
- verify_runpod_training_receipt command exists.
- Every training job has a fetchable receipt bundle.
- Fail-closed on verification failure (hash mismatch, missing items,
  duplicate item types).
- Pydantic v2 frozen + extra=forbid models.
- Deterministic bundle_hash over sorted item content hashes.
"""

from __future__ import annotations

import hashlib
import pathlib

import pytest
from quant_foundry.receipt_bundle import (
    ALLOWED_COMPRESSION,
    ALLOWED_ITEM_TYPES,
    META_FILENAME,
    ReceiptBundle,
    ReceiptBundleConfig,
    ReceiptBundleStore,
    ReceiptItem,
    compute_bundle_hash,
    compute_content_hash,
    verify_runpod_training_receipt,
)

# ---------------------------------------------------------------------------
# module imports / types
# ---------------------------------------------------------------------------


def test_module_imports_and_types() -> None:
    assert callable(ReceiptBundleStore)
    assert callable(ReceiptBundleConfig)
    assert callable(ReceiptItem)
    assert callable(ReceiptBundle)
    assert callable(compute_bundle_hash)
    assert callable(compute_content_hash)
    assert callable(verify_runpod_training_receipt)
    assert "manifest" in ALLOWED_ITEM_TYPES
    assert "oof_predictions" in ALLOWED_ITEM_TYPES
    assert "none" in ALLOWED_COMPRESSION
    assert "gzip" in ALLOWED_COMPRESSION


# ---------------------------------------------------------------------------
# ReceiptBundleConfig
# ---------------------------------------------------------------------------


def test_config_defaults() -> None:
    cfg = ReceiptBundleConfig()
    assert cfg.base_dir == "reports/runpod-training"
    assert cfg.include_manifest is True
    assert cfg.include_artifact_hash is True
    assert cfg.include_cost_report is True
    assert cfg.include_callback_log is True
    assert cfg.include_gpu_metadata is True
    assert cfg.include_oof_predictions is True
    assert cfg.compression == "none"


def test_config_frozen() -> None:
    cfg = ReceiptBundleConfig()
    with pytest.raises(Exception):
        cfg.base_dir = "other"  # type: ignore[misc]


def test_config_extra_forbid() -> None:
    with pytest.raises(Exception):
        ReceiptBundleConfig(unknown_field="x")  # type: ignore[call-arg]


def test_config_base_dir_non_empty() -> None:
    with pytest.raises(Exception):
        ReceiptBundleConfig(base_dir="")
    with pytest.raises(Exception):
        ReceiptBundleConfig(base_dir="   ")


def test_config_compression_invalid() -> None:
    with pytest.raises(Exception):
        ReceiptBundleConfig(compression="bzip2")


def test_config_compression_valid() -> None:
    for c in ("none", "gzip"):
        cfg = ReceiptBundleConfig(compression=c)
        assert cfg.compression == c


# ---------------------------------------------------------------------------
# ReceiptItem
# ---------------------------------------------------------------------------


def _valid_item() -> ReceiptItem:
    return ReceiptItem(
        item_type="manifest",
        filename="manifest.json",
        content_hash="a" * 64,
        size_bytes=10,
        included=True,
    )


def test_item_valid() -> None:
    item = _valid_item()
    assert item.item_type == "manifest"
    assert item.filename == "manifest.json"
    assert item.content_hash == "a" * 64
    assert item.size_bytes == 10
    assert item.included is True


def test_item_frozen() -> None:
    item = _valid_item()
    with pytest.raises(Exception):
        item.size_bytes = 99  # type: ignore[misc]


def test_item_extra_forbid() -> None:
    with pytest.raises(Exception):
        ReceiptItem(  # type: ignore[call-arg]
            item_type="manifest",
            filename="manifest.json",
            content_hash="a" * 64,
            size_bytes=10,
            included=True,
            extra="x",
        )


def test_item_type_non_empty() -> None:
    with pytest.raises(Exception):
        ReceiptItem(
            item_type="",
            filename="manifest.json",
            content_hash="a" * 64,
            size_bytes=10,
            included=True,
        )
    with pytest.raises(Exception):
        ReceiptItem(
            item_type="   ",
            filename="manifest.json",
            content_hash="a" * 64,
            size_bytes=10,
            included=True,
        )


def test_item_filename_non_empty() -> None:
    with pytest.raises(Exception):
        ReceiptItem(
            item_type="manifest",
            filename="",
            content_hash="a" * 64,
            size_bytes=10,
            included=True,
        )


def test_item_content_hash_must_be_hex64() -> None:
    with pytest.raises(Exception):
        ReceiptItem(
            item_type="manifest",
            filename="manifest.json",
            content_hash="xyz",
            size_bytes=10,
            included=True,
        )
    with pytest.raises(Exception):
        ReceiptItem(
            item_type="manifest",
            filename="manifest.json",
            content_hash="A" * 64,  # uppercase not allowed
            size_bytes=10,
            included=True,
        )
    with pytest.raises(Exception):
        ReceiptItem(
            item_type="manifest",
            filename="manifest.json",
            content_hash="a" * 63,  # too short
            size_bytes=10,
            included=True,
        )


def test_item_size_bytes_non_negative() -> None:
    with pytest.raises(Exception):
        ReceiptItem(
            item_type="manifest",
            filename="manifest.json",
            content_hash="a" * 64,
            size_bytes=-1,
            included=True,
        )


def test_item_size_zero_allowed() -> None:
    item = ReceiptItem(
        item_type="manifest",
        filename="manifest.json",
        content_hash=compute_content_hash(b""),
        size_bytes=0,
        included=True,
    )
    assert item.size_bytes == 0


# ---------------------------------------------------------------------------
# ReceiptBundle
# ---------------------------------------------------------------------------


def _valid_bundle() -> ReceiptBundle:
    item = _valid_item()
    return ReceiptBundle(
        bundle_id="b" * 64,
        job_id="job-1",
        dataset_id="ds-1",
        model_family="xgboost",
        created_at="2026-01-01T00:00:00+00:00",
        bundle_dir="/tmp/job-1",
        items=[item],
        bundle_hash=compute_bundle_hash([item]),
    )


def test_bundle_valid() -> None:
    b = _valid_bundle()
    assert b.job_id == "job-1"
    assert b.verified is False
    assert b.verification_error is None
    assert len(b.items) == 1


def test_bundle_frozen() -> None:
    b = _valid_bundle()
    with pytest.raises(Exception):
        b.verified = True  # type: ignore[misc]


def test_bundle_extra_forbid() -> None:
    with pytest.raises(Exception):
        ReceiptBundle(  # type: ignore[call-arg]
            bundle_id="b" * 64,
            job_id="job-1",
            dataset_id="ds-1",
            model_family="xgboost",
            created_at="2026-01-01T00:00:00+00:00",
            bundle_dir="/tmp/job-1",
            items=[_valid_item()],
            bundle_hash=compute_bundle_hash([_valid_item()]),
            unknown="x",
        )


def test_bundle_job_id_non_empty() -> None:
    with pytest.raises(Exception):
        ReceiptBundle(
            bundle_id="b" * 64,
            job_id="",
            dataset_id="ds-1",
            model_family="xgboost",
            created_at="2026-01-01T00:00:00+00:00",
            bundle_dir="/tmp/job-1",
            items=[_valid_item()],
            bundle_hash=compute_bundle_hash([_valid_item()]),
        )


def test_bundle_hash_must_be_hex64() -> None:
    with pytest.raises(Exception):
        ReceiptBundle(
            bundle_id="b" * 64,
            job_id="job-1",
            dataset_id="ds-1",
            model_family="xgboost",
            created_at="2026-01-01T00:00:00+00:00",
            bundle_dir="/tmp/job-1",
            items=[_valid_item()],
            bundle_hash="xyz",
        )


def test_bundle_items_non_empty() -> None:
    with pytest.raises(Exception):
        ReceiptBundle(
            bundle_id="b" * 64,
            job_id="job-1",
            dataset_id="ds-1",
            model_family="xgboost",
            created_at="2026-01-01T00:00:00+00:00",
            bundle_dir="/tmp/job-1",
            items=[],
            bundle_hash="a" * 64,
        )


def test_bundle_no_duplicate_item_types() -> None:
    item = _valid_item()
    with pytest.raises(Exception):
        ReceiptBundle(
            bundle_id="b" * 64,
            job_id="job-1",
            dataset_id="ds-1",
            model_family="xgboost",
            created_at="2026-01-01T00:00:00+00:00",
            bundle_dir="/tmp/job-1",
            items=[item, item],
            bundle_hash=compute_bundle_hash([item, item]),
        )


# ---------------------------------------------------------------------------
# compute_content_hash / compute_bundle_hash
# ---------------------------------------------------------------------------


def test_compute_content_hash_sha256() -> None:
    h = compute_content_hash(b"hello")
    assert h == hashlib.sha256(b"hello").hexdigest()
    assert len(h) == 64


def test_compute_content_hash_empty() -> None:
    h = compute_content_hash(b"")
    assert h == hashlib.sha256(b"").hexdigest()


def test_compute_content_hash_deterministic() -> None:
    assert compute_content_hash(b"abc") == compute_content_hash(b"abc")


def test_compute_content_hash_different_inputs() -> None:
    assert compute_content_hash(b"abc") != compute_content_hash(b"abd")


def test_compute_bundle_hash_deterministic() -> None:
    items = [
        ReceiptItem(
            item_type="manifest",
            filename="manifest.json",
            content_hash="a" * 64,
            size_bytes=1,
            included=True,
        ),
        ReceiptItem(
            item_type="cost_report",
            filename="cost_report.json",
            content_hash="b" * 64,
            size_bytes=1,
            included=True,
        ),
    ]
    h1 = compute_bundle_hash(items)
    h2 = compute_bundle_hash(items)
    assert h1 == h2
    assert len(h1) == 64


def test_compute_bundle_hash_order_independent() -> None:
    item_a = ReceiptItem(
        item_type="manifest",
        filename="manifest.json",
        content_hash="a" * 64,
        size_bytes=1,
        included=True,
    )
    item_b = ReceiptItem(
        item_type="cost_report",
        filename="cost_report.json",
        content_hash="b" * 64,
        size_bytes=1,
        included=True,
    )
    h1 = compute_bundle_hash([item_a, item_b])
    h2 = compute_bundle_hash([item_b, item_a])
    assert h1 == h2


def test_compute_bundle_hash_changes_with_content() -> None:
    item_a = ReceiptItem(
        item_type="manifest",
        filename="manifest.json",
        content_hash="a" * 64,
        size_bytes=1,
        included=True,
    )
    item_b = ReceiptItem(
        item_type="manifest",
        filename="manifest.json",
        content_hash="c" * 64,
        size_bytes=1,
        included=True,
    )
    assert compute_bundle_hash([item_a]) != compute_bundle_hash([item_b])


def test_compute_bundle_hash_empty_raises() -> None:
    with pytest.raises(ValueError):
        compute_bundle_hash([])


# ---------------------------------------------------------------------------
# ReceiptBundleStore.create_bundle
# ---------------------------------------------------------------------------


def _store(tmp_path: pathlib.Path, **kwargs: object) -> ReceiptBundleStore:
    cfg = ReceiptBundleConfig(base_dir=str(tmp_path / "reports"), **kwargs)  # type: ignore[arg-type]
    return ReceiptBundleStore(cfg)


def test_create_bundle_writes_files_and_meta(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    items = {
        "manifest": b'{"a":1}',
        "cost_report": b'{"cents":100}',
    }
    bundle = store.create_bundle("job-1", "ds-1", "xgboost", items)
    bdir = pathlib.Path(bundle.bundle_dir)
    assert bdir.is_dir()
    assert (bdir / "manifest.json").is_file()
    assert (bdir / "cost_report.json").is_file()
    assert (bdir / META_FILENAME).is_file()
    # content hashes correct
    assert bundle.items[0].content_hash == compute_content_hash(b'{"a":1}')
    assert bundle.items[1].content_hash == compute_content_hash(b'{"cents":100}')
    # bundle hash deterministic
    assert bundle.bundle_hash == compute_bundle_hash(bundle.items)
    assert bundle.verified is False


def test_create_bundle_deterministic_bundle_id(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    items = {"manifest": b"x"}
    b1 = store.create_bundle("job-1", "ds-1", "xgboost", items)
    # same job_id + same created_at -> same bundle_id
    expected = hashlib.sha256(b"job-1|" + b1.created_at.encode("utf-8")).hexdigest()
    assert b1.bundle_id == expected


def test_create_bundle_empty_job_id_raises(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.create_bundle("", "ds-1", "xgboost", {"manifest": b"x"})


def test_create_bundle_empty_items_raises(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.create_bundle("job-1", "ds-1", "xgboost", {})


def test_create_bundle_unknown_item_type_raises(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.create_bundle("job-1", "ds-1", "xgboost", {"unknown": b"x"})


def test_create_bundle_single_item(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    bundle = store.create_bundle("job-1", "ds-1", "xgboost", {"manifest": b"x"})
    assert len(bundle.items) == 1
    assert bundle.items[0].item_type == "manifest"


def test_create_bundle_empty_content(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    bundle = store.create_bundle("job-1", "ds-1", "xgboost", {"manifest": b""})
    assert bundle.items[0].size_bytes == 0
    assert bundle.items[0].content_hash == compute_content_hash(b"")


def test_create_bundle_large_content(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    big = b"0" * 100_000
    bundle = store.create_bundle("job-1", "ds-1", "xgboost", {"manifest": big})
    assert bundle.items[0].size_bytes == 100_000
    assert bundle.items[0].content_hash == compute_content_hash(big)


def test_create_bundle_gzip_compression(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path, compression="gzip")
    bundle = store.create_bundle("job-1", "ds-1", "xgboost", {"manifest": b"x" * 1000})
    bdir = pathlib.Path(bundle.bundle_dir)
    raw = (bdir / "manifest.json").read_bytes()
    # gzip magic bytes
    assert raw[:2] == b"\x1f\x8b"
    # load round-trip decompresses
    loaded = store.load_bundle(str(bdir))
    assert loaded.items[0].content_hash == compute_content_hash(b"x" * 1000)


# ---------------------------------------------------------------------------
# ReceiptBundleStore.load_bundle (round-trip)
# ---------------------------------------------------------------------------


def test_load_bundle_round_trip(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    items = {"manifest": b'{"a":1}', "cost_report": b'{"c":2}'}
    bundle = store.create_bundle("job-1", "ds-1", "xgboost", items)
    loaded = store.load_bundle(bundle.bundle_dir)
    assert loaded.job_id == bundle.job_id
    assert loaded.dataset_id == bundle.dataset_id
    assert loaded.bundle_hash == bundle.bundle_hash
    assert loaded.bundle_id == bundle.bundle_id
    assert len(loaded.items) == len(bundle.items)
    # content hashes recomputed from disk match
    for a, b in zip(loaded.items, bundle.items, strict=False):
        assert a.content_hash == b.content_hash
        assert a.size_bytes == b.size_bytes


def test_load_bundle_missing_dir_raises(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.load_bundle(str(tmp_path / "nope"))


# ---------------------------------------------------------------------------
# ReceiptBundleStore.get_bundle_path / list_bundles
# ---------------------------------------------------------------------------


def test_get_bundle_path(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    p = store.get_bundle_path("job-1")
    assert p.endswith("job-1")
    assert "reports" in p


def test_list_bundles_empty(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    assert store.list_bundles() == []


def test_list_bundles_after_create(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    store.create_bundle("job-1", "ds-1", "xgboost", {"manifest": b"x"})
    store.create_bundle("job-2", "ds-1", "xgboost", {"manifest": b"y"})
    assert store.list_bundles() == ["job-1", "job-2"]


# ---------------------------------------------------------------------------
# ReceiptBundleStore.verify_bundle
# ---------------------------------------------------------------------------


def test_verify_bundle_valid(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    bundle = store.create_bundle("job-1", "ds-1", "xgboost", {"manifest": b"x"})
    assert store.verify_bundle(bundle) is True


def test_verify_bundle_hash_mismatch_raises(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    bundle = store.create_bundle("job-1", "ds-1", "xgboost", {"manifest": b"x"})
    # Tamper with the file on disk.
    bdir = pathlib.Path(bundle.bundle_dir)
    (bdir / "manifest.json").write_bytes(b"TAMPERED")
    with pytest.raises(ValueError):
        store.verify_bundle(bundle)


def test_verify_bundle_missing_item_raises(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    bundle = store.create_bundle(
        "job-1", "ds-1", "xgboost", {"manifest": b"x", "cost_report": b"y"}
    )
    bdir = pathlib.Path(bundle.bundle_dir)
    (bdir / "cost_report.json").unlink()
    with pytest.raises(ValueError):
        store.verify_bundle(bundle)


def test_verify_bundle_corrupt_meta_bundle_hash_raises(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    bundle = store.create_bundle("job-1", "ds-1", "xgboost", {"manifest": b"x"})
    bad = bundle.model_copy(update={"bundle_hash": "0" * 64})
    with pytest.raises(ValueError):
        store.verify_bundle(bad)


# ---------------------------------------------------------------------------
# ReceiptBundleStore.delete_bundle
# ---------------------------------------------------------------------------


def test_delete_bundle_removes_dir(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    store.create_bundle("job-1", "ds-1", "xgboost", {"manifest": b"x"})
    store.delete_bundle("job-1")
    assert store.list_bundles() == []
    assert not (pathlib.Path(store.get_bundle_path("job-1"))).is_dir()


def test_delete_bundle_missing_raises(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.delete_bundle("nope")


# ---------------------------------------------------------------------------
# verify_runpod_training_receipt
# ---------------------------------------------------------------------------


def test_verify_runpod_training_receipt_valid(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    bundle = store.create_bundle("job-1", "ds-1", "xgboost", {"manifest": b"x"})
    verified = verify_runpod_training_receipt(bundle, store)
    assert verified.verified is True
    assert verified.verification_error is None


def test_verify_runpod_training_receipt_invalid_raises(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    bundle = store.create_bundle("job-1", "ds-1", "xgboost", {"manifest": b"x"})
    bdir = pathlib.Path(bundle.bundle_dir)
    (bdir / "manifest.json").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        verify_runpod_training_receipt(bundle, store)


def test_verify_runpod_training_receipt_duplicate_types_raises(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    bundle = store.create_bundle("job-1", "ds-1", "xgboost", {"manifest": b"x"})
    # Construct a bundle with duplicate item types manually.
    dup_item = bundle.items[0].model_copy()
    bad_bundle = bundle.model_copy(update={"items": [bundle.items[0], dup_item]})
    with pytest.raises(ValueError):
        verify_runpod_training_receipt(bad_bundle, store)


# ---------------------------------------------------------------------------
# Acceptance: every training job has a fetchable receipt bundle
# ---------------------------------------------------------------------------


def test_every_job_has_fetchable_bundle(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    items = {
        "manifest": b'{"m":1}',
        "artifact_hash": b"abc123",
        "cost_report": b'{"cents":5}',
        "callback_log": b"cb",
        "gpu_metadata": b'{"gpu":"A100"}',
        "oof_predictions": b"oof",
    }
    bundle = store.create_bundle("job-xyz", "ds-1", "catboost", items)
    # Fetchable: load_bundle works and verify passes.
    loaded = store.load_bundle(bundle.bundle_dir)
    assert loaded.job_id == "job-xyz"
    assert store.verify_bundle(loaded) is True
    verified = verify_runpod_training_receipt(loaded, store)
    assert verified.verified is True
