"""Tests for the end-to-end promotion pipeline scripts.

Covers:
1. The pipeline script can be imported.
2. ``run_e2e_promotion_pipeline`` produces a valid result dict.
3. ``seed_settlement_history`` writes predictions + settlements to the stores.
4. The sentinel runs without error on the seeded data.

All tests use ``tmp_path`` and do not touch real data directories.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

# ---------------------------------------------------------------------------
# Make the scripts/ directory importable so the test can import the script
# modules. The scripts add quant_foundry src to sys.path themselves on import.
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Test 1: the pipeline script can be imported.
# ---------------------------------------------------------------------------


def test_pipeline_script_importable() -> None:
    """The e2e promotion pipeline script module can be imported."""
    import run_e2e_promotion_pipeline

    assert hasattr(run_e2e_promotion_pipeline, "run_e2e_promotion_pipeline")
    assert hasattr(run_e2e_promotion_pipeline, "build_synthetic_dataset")
    assert hasattr(run_e2e_promotion_pipeline, "train_model")
    assert hasattr(run_e2e_promotion_pipeline, "create_dossier")
    assert hasattr(run_e2e_promotion_pipeline, "submit_to_gate")
    assert hasattr(run_e2e_promotion_pipeline, "run_sentinel")


def test_seed_script_importable() -> None:
    """The seed settlement history script module can be imported."""
    import seed_settlement_history

    assert hasattr(seed_settlement_history, "seed_settlement_history")


# ---------------------------------------------------------------------------
# Test 2: run_e2e_promotion_pipeline produces a valid result dict.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_close_lingering_event_loops")
class TestRunE2EPromotionPipeline:
    """Tests for the full end-to-end pipeline function."""

    def test_pipeline_produces_valid_result(self, tmp_path: pathlib.Path) -> None:
        """The pipeline returns a dict with all expected steps."""
        from run_e2e_promotion_pipeline import run_e2e_promotion_pipeline

        result = run_e2e_promotion_pipeline(output_dir=tmp_path, seed=42)

        # Top-level keys.
        assert "model_id" in result
        assert "dataset" in result
        assert "training" in result
        assert "dossier" in result
        assert "promotion_gate" in result
        assert "sentinel" in result
        assert "started_at_ns" in result
        assert "completed_at_ns" in result

        # Dataset step.
        assert result["dataset"]["row_count"] > 0
        assert result["dataset"]["parquet_path"]
        assert len(result["dataset"]["feature_names"]) > 0

        # Training step.
        assert result["training"]["artifact_id"].startswith("artifact:")
        assert len(result["training"]["sha256"]) == 64
        assert result["training"]["model_family"] == "gbm"

        # Dossier step.
        assert result["dossier"]["model_id"] == result["model_id"]
        assert result["dossier"]["status"] == "candidate"
        assert result["dossier"]["content_hash"]

        # Promotion gate step.
        assert result["promotion_gate"]["decision"] in ("approved", "rejected")
        assert result["promotion_gate"]["target_level"] == "paper_approved"

        # Sentinel step.
        assert result["sentinel"]["passed"] is True
        assert len(result["sentinel"]["checks_run"]) > 0

    def test_pipeline_writes_result_json(self, tmp_path: pathlib.Path) -> None:
        """The pipeline writes a ``pipeline_result.json`` to the output dir."""
        from run_e2e_promotion_pipeline import run_e2e_promotion_pipeline

        run_e2e_promotion_pipeline(output_dir=tmp_path, seed=42)

        result_path = tmp_path / "pipeline_result.json"
        assert result_path.exists()
        data = json.loads(result_path.read_text())
        assert data["model_id"]
        assert "dataset" in data

    def test_pipeline_gate_decision_is_approved(self, tmp_path: pathlib.Path) -> None:
        """With a passing sentinel + sufficient evidence, the gate approves."""
        from run_e2e_promotion_pipeline import run_e2e_promotion_pipeline

        result = run_e2e_promotion_pipeline(output_dir=tmp_path, seed=42)
        assert result["promotion_gate"]["decision"] == "approved"
        assert result["promotion_gate"]["rejection_reason"] is None


# ---------------------------------------------------------------------------
# Test 3 + 4: seed_settlement_history writes to stores + sentinel runs.
# ---------------------------------------------------------------------------


class TestSeedSettlementHistory:
    """Tests for the seed settlement history function."""

    def test_seed_writes_predictions_and_settlements(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """seed_settlement_history writes predictions + settlements to stores."""
        from seed_settlement_history import seed_settlement_history

        settlements_dir = tmp_path / "settlements"
        shadow_ledger_dir = tmp_path / "shadow_ledger"

        result = seed_settlement_history(
            model_id="test-seed-model",
            settlements_dir=settlements_dir,
            shadow_ledger_dir=shadow_ledger_dir,
            n_predictions=20,
            seed=42,
        )

        # Predictions were stored.
        assert result["stored_predictions"] == 20
        assert result["n_predictions"] == 20

        # Settlements were written (the JSONL file exists).
        settlement_file = settlements_dir / "test-seed-model.settlements.jsonl"
        assert settlement_file.exists()
        lines = [line for line in settlement_file.read_text().splitlines() if line.strip()]
        assert len(lines) == 20

        # Shadow predictions were written.
        shadow_file = shadow_ledger_dir / "shadow_predictions.jsonl"
        assert shadow_file.exists()
        shadow_lines = [line for line in shadow_file.read_text().splitlines() if line.strip()]
        assert len(shadow_lines) == 20

        # Settlements were settled (not pending).
        assert result["settled_count"] == 20
        assert result["mean_brier"] is not None
        assert result["mean_return_net"] is not None

    def test_sentinel_runs_on_seeded_data(self, tmp_path: pathlib.Path) -> None:
        """The sentinel runs without error on the seeded data."""
        from seed_settlement_history import seed_settlement_history

        result = seed_settlement_history(
            model_id="test-sentinel-model",
            settlements_dir=tmp_path / "settlements",
            shadow_ledger_dir=tmp_path / "shadow_ledger",
            n_predictions=15,
            seed=42,
        )

        sentinel = result["sentinel"]
        assert "passed" in sentinel
        assert "checks_run" in sentinel
        assert "issues" in sentinel
        assert isinstance(sentinel["issues"], list)
        # The sentinel should run the full battery.
        assert "shuffled_label" in sentinel["checks_run"]
        assert "time_reverse" in sentinel["checks_run"]
        assert "train_live_gap" in sentinel["checks_run"]

    def test_seed_result_is_json_serializable(self, tmp_path: pathlib.Path) -> None:
        """The seed result dict is JSON-serializable (no non-serializable types)."""
        from seed_settlement_history import seed_settlement_history

        result = seed_settlement_history(
            model_id="test-json-model",
            settlements_dir=tmp_path / "settlements",
            shadow_ledger_dir=tmp_path / "shadow_ledger",
            n_predictions=10,
            seed=42,
        )

        # Must not raise.
        serialized = json.dumps(result, default=str)
        parsed = json.loads(serialized)
        assert parsed["model_id"] == "test-json-model"
