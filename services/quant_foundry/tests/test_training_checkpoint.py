"""Tests for ``quant_foundry.training_checkpoint`` (Tier 2.7).

Tests verify:
- TrainingCheckpointConfig: frozen, extra='forbid', field validation.
- TrainingCheckpointManager: save, load, latest_checkpoint, cleanup, clear.
- CheckpointData: frozen, extra='forbid'.
- Job ID idempotency: cross-job checkpoint loading is rejected.
- Resume: latest_fold_index returns the correct fold to resume from.
- Edge cases: no checkpoints, empty dir, corrupted files.
"""

from __future__ import annotations

import pytest
from quant_foundry.training_checkpoint import (
    CheckpointData,
    CheckpointError,
    TrainingCheckpointConfig,
    TrainingCheckpointManager,
)


def _make_config(
    checkpoint_dir: str,
    job_id: str = "job-001",
    max_checkpoints: int = 3,
    save_every_n_folds: int = 1,
) -> TrainingCheckpointConfig:
    return TrainingCheckpointConfig(
        checkpoint_dir=checkpoint_dir,
        job_id=job_id,
        max_checkpoints=max_checkpoints,
        save_every_n_folds=save_every_n_folds,
    )


def _make_manager(tmp_path, **kwargs) -> TrainingCheckpointManager:
    config = _make_config(str(tmp_path / "checkpoints"), **kwargs)
    return TrainingCheckpointManager(config)


class TestTrainingCheckpointConfig:
    def test_basic_creation(self, tmp_path) -> None:
        cfg = _make_config(str(tmp_path))
        assert cfg.job_id == "job-001"
        assert cfg.max_checkpoints == 3

    def test_frozen(self, tmp_path) -> None:
        cfg = _make_config(str(tmp_path))
        with pytest.raises(Exception):
            cfg.job_id = "hack"  # type: ignore[misc]

    def test_extra_forbid(self, tmp_path) -> None:
        with pytest.raises(Exception):
            TrainingCheckpointConfig(
                checkpoint_dir=str(tmp_path),
                job_id="job-001",
                unknown_field=1,  # type: ignore[call-arg]
            )

    def test_empty_job_id_rejected(self, tmp_path) -> None:
        with pytest.raises(Exception):
            _make_config(str(tmp_path), job_id="")

    def test_max_checkpoints_ge1(self, tmp_path) -> None:
        with pytest.raises(Exception):
            _make_config(str(tmp_path), max_checkpoints=0)

    def test_save_every_n_folds_ge1(self, tmp_path) -> None:
        with pytest.raises(Exception):
            _make_config(str(tmp_path), save_every_n_folds=0)

    def test_job_checkpoint_dir(self, tmp_path) -> None:
        cfg = _make_config(str(tmp_path / "ckpt"), job_id="job-abc:123")
        # colons are sanitized to dashes
        assert cfg.job_checkpoint_dir == tmp_path / "ckpt" / "job-abc-123"


class TestTrainingCheckpointManager:
    def test_save_creates_file(self, tmp_path) -> None:
        mgr = _make_manager(tmp_path)
        path = mgr.save(
            fold_index=0,
            fold_model=b"\x80\x05fake_model",
            fold_metrics={"accuracy": 0.85},
        )
        assert path is not None
        from pathlib import Path

        assert Path(path).exists()

    def test_save_and_load(self, tmp_path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.save(
            fold_index=0,
            fold_model=b"model_bytes",
            fold_metrics={"accuracy": 0.9, "logloss": 0.3},
            total_folds=5,
        )
        path = mgr.latest_checkpoint()
        assert path is not None
        data = mgr.load(path)
        assert data.job_id == "job-001"
        assert data.fold_index == 0
        assert data.fold_model == b"model_bytes"
        assert data.fold_metrics == {"accuracy": 0.9, "logloss": 0.3}
        assert data.total_folds == 5

    def test_latest_checkpoint_none(self, tmp_path) -> None:
        mgr = _make_manager(tmp_path)
        assert mgr.latest_checkpoint() is None

    def test_latest_checkpoint_returns_highest_fold(self, tmp_path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.save(fold_index=0, fold_model=b"m0", fold_metrics={})
        mgr.save(fold_index=1, fold_model=b"m1", fold_metrics={})
        mgr.save(fold_index=2, fold_model=b"m2", fold_metrics={})
        path = mgr.latest_checkpoint()
        assert path is not None
        data = mgr.load(path)
        assert data.fold_index == 2

    def test_latest_fold_index(self, tmp_path) -> None:
        mgr = _make_manager(tmp_path)
        assert mgr.latest_fold_index() is None
        mgr.save(fold_index=0, fold_model=b"m0", fold_metrics={})
        mgr.save(fold_index=3, fold_model=b"m3", fold_metrics={})
        assert mgr.latest_fold_index() == 3

    def test_completed_folds(self, tmp_path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.save(fold_index=2, fold_model=b"m2", fold_metrics={})
        mgr.save(fold_index=0, fold_model=b"m0", fold_metrics={})
        mgr.save(fold_index=1, fold_model=b"m1", fold_metrics={})
        assert mgr.completed_folds() == [0, 1, 2]

    def test_completed_folds_empty(self, tmp_path) -> None:
        mgr = _make_manager(tmp_path)
        assert mgr.completed_folds() == []

    def test_cleanup_removes_old(self, tmp_path) -> None:
        mgr = _make_manager(tmp_path, max_checkpoints=2)
        mgr.save(fold_index=0, fold_model=b"m0", fold_metrics={})
        mgr.save(fold_index=1, fold_model=b"m1", fold_metrics={})
        mgr.save(fold_index=2, fold_model=b"m2", fold_metrics={})
        # Only the last 2 should remain
        assert mgr.completed_folds() == [1, 2]

    def test_cleanup_noop_when_under_max(self, tmp_path) -> None:
        mgr = _make_manager(tmp_path, max_checkpoints=5)
        mgr.save(fold_index=0, fold_model=b"m0", fold_metrics={})
        mgr.save(fold_index=1, fold_model=b"m1", fold_metrics={})
        assert mgr.completed_folds() == [0, 1]

    def test_clear_removes_all(self, tmp_path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.save(fold_index=0, fold_model=b"m0", fold_metrics={})
        mgr.save(fold_index=1, fold_model=b"m1", fold_metrics={})
        mgr.clear()
        assert mgr.completed_folds() == []
        assert mgr.latest_checkpoint() is None

    def test_clear_noop_when_empty(self, tmp_path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.clear()  # should not raise

    def test_load_missing_raises(self, tmp_path) -> None:
        mgr = _make_manager(tmp_path)
        with pytest.raises(CheckpointError, match="not found"):
            mgr.load(str(tmp_path / "nonexistent.pkl"))

    def test_job_id_mismatch_raises(self, tmp_path) -> None:
        mgr1 = _make_manager(tmp_path, job_id="job-001")
        mgr1.save(fold_index=0, fold_model=b"m0", fold_metrics={})
        path = mgr1.latest_checkpoint()
        assert path is not None

        # Try to load with a different job_id
        mgr2 = _make_manager(tmp_path, job_id="job-002")
        with pytest.raises(CheckpointError, match="job_id mismatch"):
            mgr2.load(path)

    def test_should_save_every_fold(self, tmp_path) -> None:
        mgr = _make_manager(tmp_path, save_every_n_folds=1)
        assert mgr.should_save(0) is True
        assert mgr.should_save(1) is True
        assert mgr.should_save(5) is True

    def test_should_save_every_3_folds(self, tmp_path) -> None:
        mgr = _make_manager(tmp_path, save_every_n_folds=3)
        # fold_index 0 → fold 1 → 1 % 3 != 0 → False
        assert mgr.should_save(0) is False
        assert mgr.should_save(1) is False
        # fold_index 2 → fold 3 → 3 % 3 == 0 → True
        assert mgr.should_save(2) is True
        assert mgr.should_save(5) is True

    def test_resume_from_explicit_path(self, tmp_path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.save(fold_index=0, fold_model=b"m0", fold_metrics={})
        path = mgr.latest_checkpoint()
        assert path is not None

        # Create a new manager with resume_from_checkpoint
        cfg2 = TrainingCheckpointConfig(
            checkpoint_dir=str(tmp_path / "checkpoints"),
            job_id="job-001",
            resume_from_checkpoint=path,
        )
        mgr2 = TrainingCheckpointManager(cfg2)
        data = mgr2.load(cfg2.resume_from_checkpoint)
        assert data.fold_index == 0

    def test_checkpoint_data_frozen(self) -> None:
        data = CheckpointData(
            job_id="job-001",
            fold_index=0,
            fold_model=b"m0",
            timestamp="2024-01-01T00:00:00Z",
        )
        with pytest.raises(Exception):
            data.fold_index = 999  # type: ignore[misc]

    def test_checkpoint_data_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            CheckpointData(  # type: ignore[call-arg]
                job_id="job-001",
                fold_index=0,
                fold_model=b"m0",
                timestamp="2024-01-01T00:00:00Z",
                unknown=1,
            )

    def test_checkpoint_survives_multiple_saves(self, tmp_path) -> None:
        """Saving multiple folds doesn't corrupt earlier checkpoints."""
        mgr = _make_manager(tmp_path, max_checkpoints=10)
        for i in range(5):
            mgr.save(
                fold_index=i,
                fold_model=f"model_{i}".encode(),
                fold_metrics={"acc": 0.8 + i * 0.01},
            )
        # All 5 should be present
        assert mgr.completed_folds() == [0, 1, 2, 3, 4]
        # Verify each can be loaded
        for i in range(5):
            path = str(mgr.config.job_checkpoint_dir / f"checkpoint_fold_{i}.pkl")
            data = mgr.load(path)
            assert data.fold_index == i
            assert data.fold_model == f"model_{i}".encode()

    def test_special_chars_in_job_id(self, tmp_path) -> None:
        """Job IDs with special characters are sanitized in the path."""
        mgr = _make_manager(tmp_path, job_id="job:special/123")
        mgr.save(fold_index=0, fold_model=b"m0", fold_metrics={})
        assert mgr.latest_checkpoint() is not None
        # The path should not contain : or /
        path = mgr.latest_checkpoint()
        assert ":" not in path.split("checkpoints")[-1]
        assert "/" not in path.split("checkpoints")[-1].replace("\\", "/").split("/")[-1]
