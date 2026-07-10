"""
quant_foundry.modules.versioning — dataset versioning with lineage tracking.

Tracks lineage between dataset versions so changes between versions are
traceable.  When a :class:`DatasetComposer` rebuilds a dataset with a
changed module config, the new version is linked to the previous one via
``parent_version_id``, and the module configuration + content hash are
recorded for tamper detection and quick config-change detection.

Public surface:
    - :class:`DatasetVersion` — a single version of a dataset
    - :class:`DatasetLineage` — ordered chain of versions for a dataset
    - :class:`DatasetVersionRegistry` — persists versions + lineage to disk
    - :func:`compute_module_config_hash` — SHA256 of a module configuration
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

# --------------------------------------------------------------------------- #
# Module config hash                                                           #
# --------------------------------------------------------------------------- #


def compute_module_config_hash(
    universe: str,
    source: str,
    sentiment: str,
    features: list[str],
    label: str,
    price_join: str,
    config: dict[str, Any] | None = None,
) -> str:
    """SHA256 hash of the module configuration.

    The hash covers the module IDs (universe, source, sentiment, features,
    label, price_join) plus the optional per-module ``config`` overrides.
    Two identical configurations produce the same hash; any change to a
    module ID or config value produces a different hash.

    Args:
        universe: Universe selector module ID (``category:id:version``).
        source: Source adapter module ID.
        sentiment: Sentiment engine module ID.
        features: List of feature computer module IDs (order-insensitive).
        label: Label computer module ID.
        price_join: Price joiner module ID.
        config: Optional per-module config overrides dict.

    Returns:
        A 64-character hex SHA256 digest.
    """
    payload = {
        "universe": universe,
        "source": source,
        "sentiment": sentiment,
        "features": sorted(features),
        "label": label,
        "price_join": price_join,
        "config": _canonical_config(config or {}),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-serializable, sortably-comparable config dict.

    Nested dicts are sorted by key; non-serializable values are stringified
    so the hash is stable regardless of insertion order.
    """
    try:
        # Round-trip through JSON with sort_keys to canonicalize.  This
        # handles nested dicts and lists deterministically when values are
        # JSON-serializable.
        return cast("dict[str, Any]", json.loads(json.dumps(config, sort_keys=True)))
    except (TypeError, ValueError):
        # Fallback: stringify any non-serializable values.
        return cast("dict[str, Any]", json.loads(json.dumps(config, sort_keys=True, default=str)))


# --------------------------------------------------------------------------- #
# Dataset version + lineage                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DatasetVersion:
    """A single version of a dataset.

    Captures everything needed to trace how a dataset version was built
    and how it relates to prior versions:

    - ``version_id``: stable identifier (``"dataset_id:v001"``).
    - ``module_config``: the ``{category: module_id}`` mapping used.
    - ``module_config_hash``: quick-compare hash of the module config.
    - ``parent_version_id``: previous version this was built from
      (``None`` for the first version).
    - ``build_mode``: ``"full"`` or ``"incremental"``.
    - ``content_hash``: hash of the parquet file for tamper detection.
    """

    version_id: str
    dataset_id: str
    version_number: int
    created_at_ns: int
    parquet_path: str
    manifest_path: str
    row_count: int
    module_config: dict[str, str] = field(default_factory=dict)
    module_config_hash: str = ""
    parent_version_id: str | None = None
    build_mode: str = "full"
    content_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DatasetVersion:
        """Deserialize from a dict (e.g. loaded from JSON)."""
        return cls(
            version_id=data["version_id"],
            dataset_id=data["dataset_id"],
            version_number=data["version_number"],
            created_at_ns=data["created_at_ns"],
            parquet_path=data["parquet_path"],
            manifest_path=data["manifest_path"],
            row_count=data["row_count"],
            module_config=dict(data.get("module_config", {})),
            module_config_hash=data.get("module_config_hash", ""),
            parent_version_id=data.get("parent_version_id"),
            build_mode=data.get("build_mode", "full"),
            content_hash=data.get("content_hash", ""),
        )


@dataclass
class DatasetLineage:
    """Lineage chain for a dataset — all versions in order.

    ``versions`` is ordered by ``version_number`` ascending.  Use
    :meth:`latest`, :meth:`get_version`, and :meth:`diff` to inspect the
    chain.
    """

    dataset_id: str
    versions: list[DatasetVersion] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Keep versions sorted by version_number for stable iteration.
        self.versions.sort(key=lambda v: v.version_number)

    def latest(self) -> DatasetVersion | None:
        """Return the latest version, or ``None`` if no versions exist."""
        if not self.versions:
            return None
        return self.versions[-1]

    def get_version(self, version_number: int) -> DatasetVersion | None:
        """Return a specific version by number, or ``None`` if absent."""
        for v in self.versions:
            if v.version_number == version_number:
                return v
        return None

    def diff(self, v1: int, v2: int) -> dict[str, Any]:
        """Compare two versions — what changed between ``v1`` and ``v2``.

        Returns a dict with:
        - ``module_changes``: ``{category: (old_id, new_id)}`` for changed
          module IDs (only categories present in both are compared).
        - ``row_count_delta``: ``row_count(v2) - row_count(v1)``.
        - ``content_changed``: ``True`` if the content hashes differ.
        - ``build_mode_changed``: ``True`` if build modes differ.
        - ``config_hash_changed``: ``True`` if module config hashes differ.

        Raises :class:`ValueError` if either version is missing.
        """
        old = self.get_version(v1)
        new = self.get_version(v2)
        if old is None:
            raise ValueError(f"version {v1} not found in dataset {self.dataset_id!r}")
        if new is None:
            raise ValueError(f"version {v2} not found in dataset {self.dataset_id!r}")

        module_changes: dict[str, tuple[str, str]] = {}
        all_categories = set(old.module_config) | set(new.module_config)
        for cat in sorted(all_categories):
            old_id = old.module_config.get(cat, "")
            new_id = new.module_config.get(cat, "")
            if old_id != new_id:
                module_changes[cat] = (old_id, new_id)

        return {
            "module_changes": module_changes,
            "row_count_delta": new.row_count - old.row_count,
            "content_changed": bool(old.content_hash) and old.content_hash != new.content_hash,
            "build_mode_changed": old.build_mode != new.build_mode,
            "config_hash_changed": old.module_config_hash != new.module_config_hash,
        }


# --------------------------------------------------------------------------- #
# Version registry                                                             #
# --------------------------------------------------------------------------- #


class DatasetVersionRegistry:
    """Manages dataset versions and lineage.

    Each dataset's versions are persisted to
    ``{registry_dir}/{dataset_id}/versions.json`` as a JSON list of
    version dicts (oldest first).
    """

    def __init__(self, registry_dir: Path) -> None:
        self.registry_dir = Path(registry_dir)
        self.registry_dir.mkdir(parents=True, exist_ok=True)

    # --- path helpers ----------------------------------------------------

    def _dataset_dir(self, dataset_id: str) -> Path:
        return self.registry_dir / dataset_id

    def _versions_path(self, dataset_id: str) -> Path:
        return self._dataset_dir(dataset_id) / "versions.json"

    # --- persistence -----------------------------------------------------

    def _load_versions(self, dataset_id: str) -> list[DatasetVersion]:
        path = self._versions_path(dataset_id)
        if not path.exists():
            return []
        body = json.loads(path.read_text(encoding="utf-8"))
        return [DatasetVersion.from_dict(item) for item in body]

    def _save_versions(self, dataset_id: str, versions: list[DatasetVersion]) -> None:
        dataset_dir = self._dataset_dir(dataset_id)
        dataset_dir.mkdir(parents=True, exist_ok=True)
        path = self._versions_path(dataset_id)
        body = [v.to_dict() for v in versions]
        path.write_text(
            json.dumps(body, sort_keys=True, indent=2),
            encoding="utf-8",
        )

    # --- public API ------------------------------------------------------

    def register_version(self, version: DatasetVersion) -> None:
        """Register a new dataset version.

        Appends the version to ``{registry_dir}/{dataset_id}/versions.json``.
        Raises :class:`ValueError` if the version number already exists.
        """
        versions = self._load_versions(version.dataset_id)
        for existing in versions:
            if existing.version_number == version.version_number:
                raise ValueError(
                    f"version {version.version_number} already registered for "
                    f"dataset {version.dataset_id!r}"
                )
        versions.append(version)
        versions.sort(key=lambda v: v.version_number)
        self._save_versions(version.dataset_id, versions)

    def get_lineage(self, dataset_id: str) -> DatasetLineage:
        """Get the full lineage chain for a dataset."""
        versions = self._load_versions(dataset_id)
        return DatasetLineage(dataset_id=dataset_id, versions=versions)

    def latest_version(self, dataset_id: str) -> DatasetVersion | None:
        """Get the latest version of a dataset, or ``None`` if none exist."""
        return self.get_lineage(dataset_id).latest()

    def next_version_number(self, dataset_id: str) -> int:
        """Get the next version number for a dataset (1 if no versions exist)."""
        latest = self.latest_version(dataset_id)
        if latest is None:
            return 1
        return latest.version_number + 1

    def list_datasets(self) -> list[str]:
        """List all dataset IDs that have at least one version."""
        if not self.registry_dir.exists():
            return []
        datasets: list[str] = []
        for entry in self.registry_dir.iterdir():
            if entry.is_dir() and self._versions_path(entry.name).exists():
                datasets.append(entry.name)
        return sorted(datasets)

    def compare_datasets(self, dataset_id_a: str, dataset_id_b: str) -> dict[str, Any]:
        """Compare the latest versions of two different datasets.

        Returns a dict with:
        - ``dataset_a`` / ``dataset_b``: the input IDs.
        - ``version_a`` / ``version_b``: latest version numbers (or ``None``).
        - ``module_changes``: ``{category: (id_a, id_b)}`` for differing
          module IDs.
        - ``row_count_delta``: ``row_count(b) - row_count(a)``.
        - ``content_changed``: ``True`` if content hashes differ.
        - ``config_hash_changed``: ``True`` if module config hashes differ.

        Raises :class:`ValueError` if either dataset has no versions.
        """
        va = self.latest_version(dataset_id_a)
        vb = self.latest_version(dataset_id_b)
        if va is None:
            raise ValueError(f"dataset {dataset_id_a!r} has no versions")
        if vb is None:
            raise ValueError(f"dataset {dataset_id_b!r} has no versions")

        module_changes: dict[str, tuple[str, str]] = {}
        all_categories = set(va.module_config) | set(vb.module_config)
        for cat in sorted(all_categories):
            id_a = va.module_config.get(cat, "")
            id_b = vb.module_config.get(cat, "")
            if id_a != id_b:
                module_changes[cat] = (id_a, id_b)

        return {
            "dataset_a": dataset_id_a,
            "dataset_b": dataset_id_b,
            "version_a": va.version_number,
            "version_b": vb.version_number,
            "module_changes": module_changes,
            "row_count_delta": vb.row_count - va.row_count,
            "content_changed": bool(va.content_hash) and va.content_hash != vb.content_hash,
            "config_hash_changed": va.module_config_hash != vb.module_config_hash,
        }


# --------------------------------------------------------------------------- #
# Helpers used by DatasetComposer integration                                  #
# --------------------------------------------------------------------------- #


def compute_content_hash(parquet_path: Path) -> str:
    """SHA256 hash of a parquet file's bytes for tamper detection."""
    h = hashlib.sha256()
    with open(parquet_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_version_id(dataset_id: str, version_number: int) -> str:
    """Build a stable version ID (``"dataset_id:v001"``)."""
    return f"{dataset_id}:v{version_number:03d}"


def now_ns() -> int:
    """Current time in nanoseconds since epoch."""
    return time.time_ns()


__all__ = [
    "DatasetLineage",
    "DatasetVersion",
    "DatasetVersionRegistry",
    "compute_content_hash",
    "compute_module_config_hash",
    "make_version_id",
    "now_ns",
]
