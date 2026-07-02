"""quant_foundry.receipt_bundle — Receipt bundle storage (Phase 6 / T-TV.3).

A *receipt bundle* is the durable, fetchable evidence packet for one
RunPod training job. Every training job MUST have a receipt bundle
stored under ``reports/runpod-training/<job-id>/`` so an operator (or
an automated promotion gate) can later re-verify the job's integrity
without reading logs.

A bundle is a directory containing one or more *items* (manifest,
artifact_hash, cost_report, callback_log, gpu_metadata,
oof_predictions, ...). Each item is a single file. The bundle carries:

- A per-item SHA-256 ``content_hash`` (so a single tampered file is
  detectable).
- A deterministic ``bundle_hash`` computed over the sorted set of item
  content hashes (so re-ordering items does not change the bundle hash,
  but any content change does).
- A ``bundle_id`` derived deterministically from ``job_id`` +
  ``created_at`` so the same job at the same timestamp always produces
  the same id.

Design (mirrors job_ledger.py / receipts.py for consistency):

- Pydantic v2 ``BaseModel`` with ``frozen=True`` and ``extra="forbid"``
  for audit integrity.
- ``pathlib`` for all file operations.
- Fail-closed verification: :func:`verify_runpod_training_receipt` and
  :meth:`ReceiptBundleStore.verify_bundle` raise ``ValueError`` on any
  mismatch (hash mismatch, missing item, duplicate item types). They
  never silently return ``True``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import pathlib
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Allowed item types for a :class:`ReceiptItem`.
ALLOWED_ITEM_TYPES: frozenset[str] = frozenset(
    {
        "manifest",
        "artifact_hash",
        "cost_report",
        "callback_log",
        "gpu_metadata",
        "oof_predictions",
    }
)

#: Allowed compression schemes.
ALLOWED_COMPRESSION: frozenset[str] = frozenset({"none", "gzip"})

#: 64-char lowercase hex regex (SHA-256).
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

#: Metadata filename written into every bundle directory.
META_FILENAME = "bundle_meta.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_content_hash(content: bytes) -> str:
    """Return the SHA-256 hex digest of ``content``.

    Args:
        content: raw bytes to hash.

    Returns:
        64-character lowercase hex string.
    """
    return hashlib.sha256(content).hexdigest()


def compute_bundle_hash(items: list[ReceiptItem]) -> str:
    """Deterministic SHA-256 over the sorted set of item content hashes.

    The hash is order-independent: two bundles with the same set of
    item hashes produce the same bundle hash regardless of the order
    in which items were supplied. Sorting is over the content_hash
    strings (lexicographic), which is deterministic.

    Args:
        items: list of :class:`ReceiptItem` (must be non-empty).

    Returns:
        64-character lowercase hex string.

    Raises:
        ValueError: if ``items`` is empty.
    """
    if not items:
        raise ValueError("compute_bundle_hash requires at least one item")
    hashes = sorted(item.content_hash for item in items)
    joined = "\n".join(hashes)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _is_hex64(value: str) -> bool:
    """True if ``value`` is a 64-char lowercase hex string."""
    return bool(_HEX64_RE.match(value))


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ReceiptBundleConfig(BaseModel):
    """Configuration for a :class:`ReceiptBundleStore`.

    Frozen + ``extra="forbid"`` for audit integrity. Controls where
    bundles are stored on disk and which optional items are expected.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_dir: str = "reports/runpod-training"
    include_manifest: bool = True
    include_artifact_hash: bool = True
    include_cost_report: bool = True
    include_callback_log: bool = True
    include_gpu_metadata: bool = True
    include_oof_predictions: bool = True
    compression: str = "none"

    @field_validator("base_dir")
    @classmethod
    def _base_dir_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("base_dir must be non-empty")
        return v

    @field_validator("compression")
    @classmethod
    def _compression_allowed(cls, v: str) -> str:
        if v not in ALLOWED_COMPRESSION:
            raise ValueError(
                f"compression must be one of {sorted(ALLOWED_COMPRESSION)}, "
                f"got {v!r}"
            )
        return v


class ReceiptItem(BaseModel):
    """One file inside a receipt bundle.

    ``content_hash`` is the SHA-256 of the item's bytes at write time.
    ``included`` records whether the item was actually written to disk
    (an item may be declared but excluded if, e.g., the job produced no
    OOF predictions).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_type: str
    filename: str
    content_hash: str
    size_bytes: int
    included: bool

    @field_validator("item_type")
    @classmethod
    def _item_type_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("item_type must be non-empty")
        return v

    @field_validator("filename")
    @classmethod
    def _filename_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("filename must be non-empty")
        return v

    @field_validator("content_hash")
    @classmethod
    def _content_hash_hex64(cls, v: str) -> str:
        if not _is_hex64(v):
            raise ValueError(
                "content_hash must be a 64-character lowercase hex string "
                "(SHA-256)"
            )
        return v

    @field_validator("size_bytes")
    @classmethod
    def _size_bytes_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("size_bytes must be >= 0")
        return v


class ReceiptBundle(BaseModel):
    """A complete receipt bundle for one training job.

    ``bundle_id`` is deterministic: SHA-256 over ``job_id`` + ``created_at``.
    ``bundle_hash`` is deterministic over the sorted set of item content
    hashes (see :func:`compute_bundle_hash`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bundle_id: str
    job_id: str
    dataset_id: str
    model_family: str
    created_at: str
    bundle_dir: str
    items: list[ReceiptItem] = Field(min_length=1)
    bundle_hash: str
    verified: bool = False
    verification_error: str | None = None

    @field_validator("job_id")
    @classmethod
    def _job_id_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("job_id must be non-empty")
        return v

    @field_validator("bundle_hash")
    @classmethod
    def _bundle_hash_hex64(cls, v: str) -> str:
        if not _is_hex64(v):
            raise ValueError(
                "bundle_hash must be a 64-character lowercase hex string "
                "(SHA-256)"
            )
        return v

    @field_validator("items")
    @classmethod
    def _items_non_empty(cls, v: list[ReceiptItem]) -> list[ReceiptItem]:
        if not v:
            raise ValueError("items must contain at least one ReceiptItem")
        return v

    @field_validator("items")
    @classmethod
    def _no_duplicate_item_types(
        cls, v: list[ReceiptItem]
    ) -> list[ReceiptItem]:
        seen: set[str] = set()
        for item in v:
            if item.item_type in seen:
                raise ValueError(
                    f"duplicate item_type {item.item_type!r} in bundle items"
                )
            seen.add(item.item_type)
        return v


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ReceiptBundleStore:
    """Filesystem-backed store for receipt bundles.

    Bundles are stored under ``<base_dir>/<job_id>/``. Each bundle
    directory contains the item files plus a ``bundle_meta.json`` file
    carrying the full :class:`ReceiptBundle` metadata so the bundle can
    be re-loaded without recomputing hashes.

    The store is fail-closed: verification raises ``ValueError`` on any
    mismatch rather than returning ``False``.
    """

    def __init__(self, config: ReceiptBundleConfig) -> None:
        """Initialize the store.

        Args:
            config: store configuration (base_dir, compression, ...).
        """
        self.config = config
        self.base_dir = pathlib.Path(config.base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # --- path helpers ---

    def get_bundle_path(self, job_id: str) -> str:
        """Return the path to the bundle directory for ``job_id``.

        Does NOT imply the directory exists.

        Args:
            job_id: training job id.

        Returns:
            String path ``<base_dir>/<job_id>``.
        """
        return str(self.base_dir / job_id)

    def list_bundles(self) -> list[str]:
        """List all job_ids that have a bundle directory.

        Returns:
            Sorted list of job_ids (strings).
        """
        if not self.base_dir.is_dir():
            return []
        job_ids: list[str] = []
        for child in self.base_dir.iterdir():
            if child.is_dir():
                job_ids.append(child.name)
        return sorted(job_ids)

    # --- create / load ---

    def create_bundle(
        self,
        job_id: str,
        dataset_id: str,
        model_family: str,
        items: dict[str, bytes],
    ) -> ReceiptBundle:
        """Create and persist a receipt bundle for ``job_id``.

        Writes each item to disk under ``<base_dir>/<job_id>/``,
        computes per-item content hashes, computes the deterministic
        bundle hash, writes ``bundle_meta.json``, and returns the
        :class:`ReceiptBundle`.

        Args:
            job_id: training job id (non-empty).
            dataset_id: dataset id used for training.
            model_family: model family name (e.g. "xgboost").
            items: mapping of item_type -> bytes. Keys must be in
                :data:`ALLOWED_ITEM_TYPES`. At least one item is
                required. Item types must be unique (dict keys enforce
                this naturally).

        Returns:
            The persisted :class:`ReceiptBundle` (``verified=False``).

        Raises:
            ValueError: if ``job_id`` is empty, ``items`` is empty, or
                an item type is not allowed.
        """
        if not job_id or not job_id.strip():
            raise ValueError("job_id must be non-empty")
        if not items:
            raise ValueError("items must contain at least one entry")
        bad = [k for k in items if k not in ALLOWED_ITEM_TYPES]
        if bad:
            raise ValueError(
                f"unknown item types: {bad!r} (allowed: "
                f"{sorted(ALLOWED_ITEM_TYPES)})"
            )

        bundle_dir = self.base_dir / job_id
        bundle_dir.mkdir(parents=True, exist_ok=False)

        receipt_items: list[ReceiptItem] = []
        for item_type, content in items.items():
            filename = self._filename_for(item_type)
            self._write_item(bundle_dir / filename, content)
            receipt_items.append(
                ReceiptItem(
                    item_type=item_type,
                    filename=filename,
                    content_hash=compute_content_hash(content),
                    size_bytes=len(content),
                    included=True,
                )
            )

        # Deterministic ordering for bundle_hash.
        bundle_hash = compute_bundle_hash(receipt_items)
        created_at = self._now_iso()
        bundle_id = self._compute_bundle_id(job_id, created_at)

        bundle = ReceiptBundle(
            bundle_id=bundle_id,
            job_id=job_id,
            dataset_id=dataset_id,
            model_family=model_family,
            created_at=created_at,
            bundle_dir=str(bundle_dir),
            items=receipt_items,
            bundle_hash=bundle_hash,
            verified=False,
            verification_error=None,
        )

        self._write_meta(bundle_dir, bundle)
        return bundle

    def load_bundle(self, bundle_dir: str) -> ReceiptBundle:
        """Load a bundle from ``bundle_dir``.

        Reads ``bundle_meta.json`` and re-reads every item file from
        disk, recomputing content hashes. The returned bundle reflects
        the CURRENT on-disk state (so a tampered file will show a
        content hash mismatch when verified).

        Args:
            bundle_dir: path to the bundle directory.

        Returns:
            The :class:`ReceiptBundle` loaded from disk.

        Raises:
            FileNotFoundError: if the bundle directory or meta file is
                missing.
            ValueError: if the meta file is corrupt.
        """
        bdir = pathlib.Path(bundle_dir)
        meta_path = bdir / META_FILENAME
        if not meta_path.is_file():
            raise FileNotFoundError(
                f"bundle meta not found: {meta_path}"
            )
        with meta_path.open("r", encoding="utf-8") as fh:
            meta = json.load(fh)
        # Re-read items from disk to reflect current state.
        loaded_items: list[ReceiptItem] = []
        for item_meta in meta.get("items", []):
            filename = item_meta["filename"]
            item_path = bdir / filename
            if item_path.is_file():
                content = self._read_item(item_path)
                content_hash = compute_content_hash(content)
                size_bytes = len(content)
                included = True
            else:
                content_hash = item_meta["content_hash"]
                size_bytes = item_meta["size_bytes"]
                included = False
            loaded_items.append(
                ReceiptItem(
                    item_type=item_meta["item_type"],
                    filename=filename,
                    content_hash=content_hash,
                    size_bytes=size_bytes,
                    included=included,
                )
            )
        return ReceiptBundle(
            bundle_id=meta["bundle_id"],
            job_id=meta["job_id"],
            dataset_id=meta["dataset_id"],
            model_family=meta["model_family"],
            created_at=meta["created_at"],
            bundle_dir=str(bdir),
            items=loaded_items,
            bundle_hash=meta["bundle_hash"],
            verified=meta.get("verified", False),
            verification_error=meta.get("verification_error"),
        )

    # --- verify ---

    def verify_bundle(self, bundle: ReceiptBundle) -> bool:
        """Verify a bundle's integrity against on-disk content.

        Re-reads every item file, recomputes its content hash, and
        checks it matches the recorded hash. Then recomputes the
        bundle hash and checks it matches the recorded bundle hash.

        Fail-closed: raises ``ValueError`` on any mismatch (hash
        mismatch, missing item file, duplicate item types). Returns
        ``True`` only if every check passes.

        Args:
            bundle: the bundle to verify.

        Returns:
            ``True`` if verified.

        Raises:
            ValueError: if verification fails (hash mismatch, missing
                item, duplicate item types).
        """
        # Duplicate item types check (fail-closed).
        seen: set[str] = set()
        for item in bundle.items:
            if item.item_type in seen:
                raise ValueError(
                    f"duplicate item_type {item.item_type!r} (fail-closed)"
                )
            seen.add(item.item_type)

        bdir = pathlib.Path(bundle.bundle_dir)
        current_hashes: list[str] = []
        for item in bundle.items:
            item_path = bdir / item.filename
            if not item_path.is_file():
                raise ValueError(
                    f"missing item file for {item.item_type!r}: "
                    f"{item_path} (fail-closed)"
                )
            content = self._read_item(item_path)
            actual = compute_content_hash(content)
            if actual != item.content_hash:
                raise ValueError(
                    f"content hash mismatch for {item.item_type!r}: "
                    f"recorded={item.content_hash} actual={actual} "
                    f"(fail-closed)"
                )
            current_hashes.append(item.content_hash)

        # Recompute bundle hash over the current item hashes.
        expected_bundle_hash = compute_bundle_hash(bundle.items)
        if expected_bundle_hash != bundle.bundle_hash:
            raise ValueError(
                f"bundle hash mismatch: recorded={bundle.bundle_hash} "
                f"actual={expected_bundle_hash} (fail-closed)"
            )
        return True

    # --- delete ---

    def delete_bundle(self, job_id: str) -> None:
        """Delete the bundle directory for ``job_id``.

        Fail-closed: raises ``FileNotFoundError`` if the bundle does not
        exist.

        Args:
            job_id: training job id.

        Raises:
            FileNotFoundError: if no bundle exists for ``job_id``.
        """
        bundle_dir = self.base_dir / job_id
        if not bundle_dir.is_dir():
            raise FileNotFoundError(
                f"bundle directory does not exist: {bundle_dir}"
            )
        # Remove all contents then the directory itself.
        for child in bundle_dir.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink()
            elif child.is_dir():
                # Recurse for any unexpected subdirs.
                self._remove_tree(child)
        bundle_dir.rmdir()

    # --- internal helpers ---

    @staticmethod
    def _remove_tree(path: pathlib.Path) -> None:
        for child in path.iterdir():
            if child.is_dir():
                ReceiptBundleStore._remove_tree(child)
            else:
                child.unlink()
        path.rmdir()

    def _filename_for(self, item_type: str) -> str:
        """Return the on-disk filename for an item type."""
        return f"{item_type}.json"

    def _write_item(self, path: pathlib.Path, content: bytes) -> None:
        """Write item content to disk, applying compression if configured."""
        if self.config.compression == "gzip":
            with path.open("wb") as fh:
                fh.write(gzip.compress(content))
        else:
            with path.open("wb") as fh:
                fh.write(content)

    def _read_item(self, path: pathlib.Path) -> bytes:
        """Read item content from disk, decompressing if configured."""
        if self.config.compression == "gzip":
            with path.open("rb") as fh:
                return gzip.decompress(fh.read())
        with path.open("rb") as fh:
            return fh.read()

    def _write_meta(
        self, bundle_dir: pathlib.Path, bundle: ReceiptBundle
    ) -> None:
        meta: dict[str, Any] = {
            "bundle_id": bundle.bundle_id,
            "job_id": bundle.job_id,
            "dataset_id": bundle.dataset_id,
            "model_family": bundle.model_family,
            "created_at": bundle.created_at,
            "bundle_dir": bundle.bundle_dir,
            "bundle_hash": bundle.bundle_hash,
            "verified": bundle.verified,
            "verification_error": bundle.verification_error,
            "items": [item.model_dump() for item in bundle.items],
        }
        meta_path = bundle_dir / META_FILENAME
        with meta_path.open("w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, sort_keys=True)

    @staticmethod
    def _compute_bundle_id(job_id: str, created_at: str) -> str:
        """Deterministic bundle id: SHA-256 over job_id + created_at."""
        h = hashlib.sha256()
        h.update(job_id.encode("utf-8"))
        h.update(b"|")
        h.update(created_at.encode("utf-8"))
        return h.hexdigest()

    @staticmethod
    def _now_iso() -> str:
        """Current UTC timestamp in ISO 8601 format (deterministic-ish)."""
        import datetime

        return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Verification command
# ---------------------------------------------------------------------------


def verify_runpod_training_receipt(
    bundle: ReceiptBundle, store: ReceiptBundleStore
) -> ReceiptBundle:
    """Verify a RunPod training receipt bundle.

    Convenience wrapper around :meth:`ReceiptBundleStore.verify_bundle`
    that returns a new :class:`ReceiptBundle` with ``verified=True`` on
    success. Fail-closed: raises ``ValueError`` if verification fails.

    Args:
        bundle: the bundle to verify.
        store: the store that owns the bundle's on-disk files.

    Returns:
        A new :class:`ReceiptBundle` with ``verified=True`` and
        ``verification_error=None``.

    Raises:
        ValueError: if verification fails (hash mismatch, missing
            item, duplicate item types).
    """
    store.verify_bundle(bundle)
    return bundle.model_copy(
        update={"verified": True, "verification_error": None}
    )
