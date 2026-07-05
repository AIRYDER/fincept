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
        """No dataset_load_spec → gate skipped (inline CSV path)."""
        # Use inline_dataset_csv to bypass the manifest-first load.
        inp = _make_training_input(
            "pit-no-spec-1",
            inline_dataset_csv="feature_1,feature_2,label\n1.0,2.0,0\n3.0,4.0,1\n",
            extra_constraints={"training_mode": "production"},
        )
        result = handler_module.handler(inp)
        # No manifest loaded → no pit_proof check → should not block.
        assert result.get("error_code") != "pit_proof_not_verified"

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
