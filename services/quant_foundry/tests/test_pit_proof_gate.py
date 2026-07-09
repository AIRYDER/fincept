"""Integration tests for Tier 1.5: PIT proof gate in the handler.

Verifies that the handler enforces the dataset manifest's
``pit_proof_verified`` flag:

1. **Production mode + pit_proof_verified=False** → fail-closed with
   ``error_code="pit_proof_not_verified"`` and a signed failure callback.
   Training does NOT start.
2. **Production mode + pit_proof_verified=True** → training proceeds
   normally.
3. **Canary/research mode + pit_proof_verified=False** → advisory warning
   logged, training continues (permissive by design).
4. **No manifest loaded (inline CSV or volume path)** → gate is skipped
   (no ``pit_proof_verified`` field to check).

The handler module lives in ``runpod/quant-foundry-training/handler.py``
(outside the quant_foundry package), so tests add that directory to
``sys.path`` and import the module directly. All tests use the canary /
LocalTrainer path (no GPU/ML deps needed).
"""

from __future__ import annotations

import importlib
import json
import pathlib
import sys
import tempfile

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_HANDLER_DIR = str(_REPO_ROOT / "runpod" / "quant-foundry-training")


@pytest.fixture(scope="module")
def handler_module():
    """Import the handler module (adding its dir to sys.path)."""
    if _HANDLER_DIR not in sys.path:
        sys.path.insert(0, _HANDLER_DIR)
    return importlib.import_module("handler")


def _make_training_input(job_id: str, **extra) -> dict:
    """Build a minimal training input dict for the handler (canary path)."""
    return {
        "input": {
            "job_id": job_id,
            "dataset_manifest_ref": "ds-manifest-test",
            "model_family": "gbm",
            "search_space": {},
            "random_seed": 42,
            "hardware_class": "mock-gpu",
            "extra_constraints": {},
            **extra,
        }
    }


def _make_load_spec(
    *,
    manifest_dict: dict,
    data_csv: str = "feature_1,feature_2,label\n1.0,2.0,0\n3.0,4.0,1\n",
) -> dict:
    """Build a dataset_load_spec with an inline manifest + inline data.

    The manifest is written to a temp file and referenced via
    ``manifest_uri``. The data is written to a temp CSV file and
    referenced via ``data_uri``. Both use ``file://`` URIs so the
    ManifestDatasetLoader can fetch them locally.
    """
    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="qf_pit_test_"))
    manifest_path = tmp_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_dict), encoding="utf-8")
    data_path = tmp_dir / "data.csv"
    data_path.write_text(data_csv, encoding="utf-8")

    import hashlib

    manifest_bytes = manifest_path.read_bytes()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    data_bytes = data_path.read_bytes()
    data_sha = hashlib.sha256(data_bytes).hexdigest()

    return {
        "manifest_uri": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "data_uri": str(data_path),
        "data_sha256": data_sha,
        "data_format": "csv",
        "row_count": 2,
        "feature_schema_hash": manifest_dict.get("feature_schema_hash", ""),
        "label_schema_hash": manifest_dict.get("label_schema_hash", ""),
    }


def _make_valid_manifest(
    *,
    pit_proof_verified: bool = True,
) -> dict:
    """Build a valid manifest dict with the given pit_proof_verified flag."""
    return {
        "schema_version": 1,
        "dataset_id": "pit-test-dataset",
        "feature_schema_hash": "a" * 64,
        "label_schema_hash": "b" * 64,
        "as_of_ts": 1700000000_000_000_000,
        "universe_hash": "c" * 64,
        "row_count": 2,
        "checksum": "d" * 64,
        "folds": {},
        "pit_proof_verified": pit_proof_verified,
        "source_vintage_refs": [],
        "quality_report_hash": None,
        "manifest_uri": "",
        "data_uri": "",
        "data_format": "csv",
        "data_sha256": "",
        "quality_report_uri": None,
        "quality_report_sha256": None,
        "feature_names": ["feature_1", "feature_2"],
    }


# --------------------------------------------------------------------------- #
# PIT proof gate tests                                                         #
# --------------------------------------------------------------------------- #


class TestPitProofGate:
    """Tests for the PIT proof gate in the handler."""

    def test_production_mode_blocks_when_pit_proof_false(
        self, handler_module, tmp_path: pathlib.Path
    ) -> None:
        """Production mode + pit_proof_verified=False → fail-closed."""
        manifest = _make_valid_manifest(pit_proof_verified=False)
        load_spec = _make_load_spec(manifest_dict=manifest)
        inp = _make_training_input(
            "pit-prod-block-1",
            dataset_load_spec=load_spec,
            extra_constraints={"training_mode": "production"},
        )
        result = handler_module.handler(inp)
        assert result.get("error_code") == "pit_proof_not_verified"
        assert "pit_proof" in result.get("error_summary", "").lower() or (
            "point-in-time" in result.get("error_summary", "").lower()
        )

    def test_production_mode_proceeds_when_pit_proof_true(
        self, handler_module, tmp_path: pathlib.Path
    ) -> None:
        """Production mode + pit_proof_verified=True → training proceeds."""
        manifest = _make_valid_manifest(pit_proof_verified=True)
        load_spec = _make_load_spec(manifest_dict=manifest)
        inp = _make_training_input(
            "pit-prod-ok-1",
            dataset_load_spec=load_spec,
            extra_constraints={"training_mode": "production"},
        )
        result = handler_module.handler(inp)
        # Should NOT have the pit_proof error code.
        assert result.get("error_code") != "pit_proof_not_verified"

    def test_research_mode_advisory_when_pit_proof_false(
        self, handler_module, tmp_path: pathlib.Path
    ) -> None:
        """Research mode + pit_proof_verified=False → advisory, continues."""
        manifest = _make_valid_manifest(pit_proof_verified=False)
        load_spec = _make_load_spec(manifest_dict=manifest)
        inp = _make_training_input(
            "pit-research-advisory-1",
            dataset_load_spec=load_spec,
            extra_constraints={"training_mode": "research"},
        )
        result = handler_module.handler(inp)
        # Research mode should NOT block — training proceeds.
        assert result.get("error_code") != "pit_proof_not_verified"

    def test_canary_mode_advisory_when_pit_proof_false(
        self, handler_module, tmp_path: pathlib.Path
    ) -> None:
        """Canary mode + pit_proof_verified=False → advisory, continues."""
        manifest = _make_valid_manifest(pit_proof_verified=False)
        load_spec = _make_load_spec(manifest_dict=manifest)
        inp = _make_training_input(
            "pit-canary-advisory-1",
            dataset_load_spec=load_spec,
            extra_constraints={"training_mode": "canary"},
        )
        result = handler_module.handler(inp)
        # Canary mode should NOT block — training proceeds.
        assert result.get("error_code") != "pit_proof_not_verified"

    def test_no_load_spec_skips_pit_gate(
        self, handler_module, tmp_path: pathlib.Path
    ) -> None:
        """No dataset_load_spec → gate skipped (inline CSV path, canary only)."""
        # Use inline_dataset_csv with canary mode (production mode is
        # rejected by the inline_dataset_csv production guard).
        inp = _make_training_input(
            "pit-no-spec-1",
            inline_dataset_csv="feature_1,feature_2,label\n1.0,2.0,0\n3.0,4.0,1\n",
            extra_constraints={"training_mode": "canary"},
        )
        result = handler_module.handler(inp)
        # No manifest loaded → no pit_proof check → should not block.
        assert result.get("error_code") != "pit_proof_not_verified"

    def test_inline_dataset_csv_rejected_in_production(
        self, handler_module, tmp_path: pathlib.Path
    ) -> None:
        """Tier 1.5: inline_dataset_csv is test-only — production rejects it."""
        inp = _make_training_input(
            "pit-inline-prod-1",
            inline_dataset_csv="feature_1,feature_2,label\n1.0,2.0,0\n3.0,4.0,1\n",
            extra_constraints={"training_mode": "production"},
        )
        result = handler_module.handler(inp)
        # Production mode must reject inline_dataset_csv with a signed
        # failure receipt — it bypasses manifest hashes, PIT proof, and
        # the dataset registry.
        assert result.get("error_code") == "inline_dataset_csv_in_production"

    def test_production_mode_blocks_when_pit_proof_missing(
        self, handler_module, tmp_path: pathlib.Path
    ) -> None:
        """Production mode + pit_proof_verified field missing → fail-closed."""
        manifest = _make_valid_manifest(pit_proof_verified=True)
        del manifest["pit_proof_verified"]  # Remove the field entirely
        load_spec = _make_load_spec(manifest_dict=manifest)
        inp = _make_training_input(
            "pit-prod-missing-1",
            dataset_load_spec=load_spec,
            extra_constraints={"training_mode": "production"},
        )
        result = handler_module.handler(inp)
        # Missing field → pit_flag is None → not True → fail-closed.
        assert result.get("error_code") == "pit_proof_not_verified"


# --------------------------------------------------------------------------- #
# Tier 1.5: dataset_load_spec required for production (Gap 4)                 #
# --------------------------------------------------------------------------- #


class TestDatasetLoadSpecRequiredForProduction:
    """Production mode must use dataset_load_spec — no fallback paths."""

    def test_production_without_dataset_load_spec_rejected(
        self, handler_module, tmp_path: pathlib.Path
    ) -> None:
        """Production mode + no dataset_load_spec → fail-closed."""
        inp = _make_training_input(
            "prod-no-spec-1",
            extra_constraints={"training_mode": "production"},
        )
        result = handler_module.handler(inp)
        assert result.get("error_code") == "dataset_load_spec_required_for_production"

    def test_production_with_empty_dataset_load_spec_rejected(
        self, handler_module, tmp_path: pathlib.Path
    ) -> None:
        """Production mode + empty dataset_load_spec dict → fail-closed."""
        inp = _make_training_input(
            "prod-empty-spec-1",
            dataset_load_spec={},
            extra_constraints={"training_mode": "production"},
        )
        result = handler_module.handler(inp)
        # An empty dict is falsy in Python, so the guard fires.
        assert result.get("error_code") == "dataset_load_spec_required_for_production"

    def test_canary_without_dataset_load_spec_proceeds(
        self, handler_module, tmp_path: pathlib.Path
    ) -> None:
        """Canary mode + no dataset_load_spec → training proceeds (permissive)."""
        inp = _make_training_input(
            "canary-no-spec-1",
            extra_constraints={"training_mode": "canary"},
        )
        result = handler_module.handler(inp)
        assert result.get("error_code") != "dataset_load_spec_required_for_production"

    def test_research_without_dataset_load_spec_proceeds(
        self, handler_module, tmp_path: pathlib.Path
    ) -> None:
        """Research mode + no dataset_load_spec → training proceeds."""
        inp = _make_training_input(
            "research-no-spec-1",
            extra_constraints={"training_mode": "research"},
        )
        result = handler_module.handler(inp)
        assert result.get("error_code") != "dataset_load_spec_required_for_production"


# --------------------------------------------------------------------------- #
# Tier 1.5: dataset registry dispatch gate (Gap 2)                            #
# --------------------------------------------------------------------------- #


class TestDatasetRegistryDispatchGate:
    """Production mode must pass the dataset registry dispatch gate."""

    def test_production_rejects_unregistered_dataset(
        self, handler_module, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        """Production mode + unregistered dataset_id → fail-closed."""
        # Create an empty registry file (no entries).
        registry_path = tmp_path / "registry.jsonl"
        registry_path.write_text("", encoding="utf-8")
        monkeypatch.setenv("QUANT_FOUNDRY_DATASET_REGISTRY_PATH", str(registry_path))

        manifest = _make_valid_manifest(pit_proof_verified=True)
        manifest["dataset_id"] = "unregistered-dataset-123"
        load_spec = _make_load_spec(manifest_dict=manifest)
        load_spec["dataset_id"] = "unregistered-dataset-123"
        inp = _make_training_input(
            "prod-unregistered-1",
            dataset_load_spec=load_spec,
            extra_constraints={"training_mode": "production"},
        )
        result = handler_module.handler(inp)
        assert result.get("error_code") == "dataset_registry_dispatch_rejected"

    def test_production_rejects_low_readiness_dataset(
        self, handler_module, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        """Production mode + L1 dataset → fail-closed (requires L3+)."""
        from quant_foundry.dataset_manifest import DatasetRegistry, ReadinessLevel

        registry_path = tmp_path / "registry.jsonl"
        registry = DatasetRegistry(path=registry_path)
        # Register a dataset at L1 (too low for production).
        registry.register(
            dataset_id="low-readiness-ds",
            manifest_uri="file:///manifest.json",
            data_uri="file:///data.parquet",
            readiness_level=ReadinessLevel.L1_RAW,
        )

        monkeypatch.setenv("QUANT_FOUNDRY_DATASET_REGISTRY_PATH", str(registry_path))

        manifest = _make_valid_manifest(pit_proof_verified=True)
        manifest["dataset_id"] = "low-readiness-ds"
        load_spec = _make_load_spec(manifest_dict=manifest)
        load_spec["dataset_id"] = "low-readiness-ds"
        inp = _make_training_input(
            "prod-low-readiness-1",
            dataset_load_spec=load_spec,
            extra_constraints={"training_mode": "production"},
        )
        result = handler_module.handler(inp)
        assert result.get("error_code") == "dataset_registry_dispatch_rejected"

    def test_production_accepts_l3_registered_dataset(
        self, handler_module, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        """Production mode + L3 registered dataset → registry gate passes."""
        from quant_foundry.dataset_manifest import DatasetRegistry, ReadinessLevel

        registry_path = tmp_path / "registry.jsonl"
        registry = DatasetRegistry(path=registry_path)
        # Register at L1, then promote to L2.
        registry.register(
            dataset_id="l3-prod-ds",
            manifest_uri="file:///manifest.json",
            data_uri="file:///data.parquet",
            readiness_level=ReadinessLevel.L1_RAW,
        )
        registry.promote_readiness("l3-prod-ds", ReadinessLevel.L2_VALIDATED)
        # L3+ requires a quality report URI + hash on the entry. Patch the
        # entry to add them before promoting to L3.
        entry = registry.inspect("l3-prod-ds")
        updated = entry.model_copy(update={
            "quality_report_uri": "file:///quality_report.json",
            "quality_report_sha256": "e" * 64,
        })
        registry._entries["l3-prod-ds"][-1] = updated
        if registry._path is not None:
            registry._rewrite_ledger()
        # Now promote to L3.
        registry.promote_readiness(
            "l3-prod-ds",
            ReadinessLevel.L3_QUALITY_GATED,
        )

        monkeypatch.setenv("QUANT_FOUNDRY_DATASET_REGISTRY_PATH", str(registry_path))

        manifest = _make_valid_manifest(pit_proof_verified=True)
        manifest["dataset_id"] = "l3-prod-ds"
        load_spec = _make_load_spec(manifest_dict=manifest)
        load_spec["dataset_id"] = "l3-prod-ds"
        inp = _make_training_input(
            "prod-l3-ok-1",
            dataset_load_spec=load_spec,
            extra_constraints={"training_mode": "production"},
        )
        result = handler_module.handler(inp)
        # Should NOT be rejected by the registry gate.
        assert result.get("error_code") != "dataset_registry_dispatch_rejected"

    def test_production_no_registry_path_advisory(
        self, handler_module, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        """Production mode + no registry path configured → advisory, continues."""
        monkeypatch.delenv("QUANT_FOUNDRY_DATASET_REGISTRY_PATH", raising=False)

        manifest = _make_valid_manifest(pit_proof_verified=True)
        load_spec = _make_load_spec(manifest_dict=manifest)
        inp = _make_training_input(
            "prod-no-registry-1",
            dataset_load_spec=load_spec,
            extra_constraints={"training_mode": "production"},
        )
        result = handler_module.handler(inp)
        # Should NOT be rejected — advisory only.
        assert result.get("error_code") != "dataset_registry_dispatch_rejected"

    def test_canary_skips_registry_gate(
        self, handler_module, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        """Canary mode → registry gate is skipped entirely."""
        # Even with a registry path set, canary should not be rejected.
        registry_path = tmp_path / "registry.jsonl"
        registry_path.write_text("", encoding="utf-8")
        monkeypatch.setenv("QUANT_FOUNDRY_DATASET_REGISTRY_PATH", str(registry_path))

        manifest = _make_valid_manifest(pit_proof_verified=True)
        load_spec = _make_load_spec(manifest_dict=manifest)
        inp = _make_training_input(
            "canary-registry-skip-1",
            dataset_load_spec=load_spec,
            extra_constraints={"training_mode": "canary"},
        )
        result = handler_module.handler(inp)
        assert result.get("error_code") != "dataset_registry_dispatch_rejected"

    def test_production_rejects_deprecated_dataset(
        self, handler_module, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        """Production mode + deprecated dataset → fail-closed."""
        from quant_foundry.dataset_manifest import DatasetRegistry, ReadinessLevel

        registry_path = tmp_path / "registry.jsonl"
        registry = DatasetRegistry(path=registry_path)
        registry.register(
            dataset_id="deprecated-ds",
            manifest_uri="file:///manifest.json",
            data_uri="file:///data.parquet",
            readiness_level=ReadinessLevel.L1_RAW,
        )
        # Mark the entry as deprecated by patching its status.
        entry = registry.inspect("deprecated-ds")
        from quant_foundry.dataset_manifest import RegistryStatus

        updated = entry.model_copy(update={"status": RegistryStatus.DEPRECATED})
        registry._entries["deprecated-ds"][-1] = updated
        if registry._path is not None:
            registry._rewrite_ledger()

        monkeypatch.setenv("QUANT_FOUNDRY_DATASET_REGISTRY_PATH", str(registry_path))

        manifest = _make_valid_manifest(pit_proof_verified=True)
        manifest["dataset_id"] = "deprecated-ds"
        load_spec = _make_load_spec(manifest_dict=manifest)
        load_spec["dataset_id"] = "deprecated-ds"
        inp = _make_training_input(
            "prod-deprecated-1",
            dataset_load_spec=load_spec,
            extra_constraints={"training_mode": "production"},
        )
        result = handler_module.handler(inp)
        assert result.get("error_code") == "dataset_registry_dispatch_rejected"
