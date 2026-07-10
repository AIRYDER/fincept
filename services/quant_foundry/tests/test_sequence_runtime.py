"""Tests for quant_foundry.sequence_runtime (T-10.5).

Covers the PyTorch sequence runtime: the Docker image spec, checkpoint
config, mixed precision config, metrics artifact, the sequence tensor
loader, the checkpoint manager, the mixed precision manager, and the
sequence healthcheck.

The test host is CPU-only (torch is installed with the CPU index URL), so
GPU-dependent assertions check the "no GPU" degradation path. The tensor
load, checkpoint round-trip, mixed precision (CPU mode), and forward pass
run on CPU.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("torch")

from quant_foundry.sequence_runtime import (
    CheckpointConfig,
    CheckpointManager,
    MetricsArtifact,
    MixedPrecisionConfig,
    MixedPrecisionManager,
    SequenceHealthcheck,
    SequenceImageSpec,
    SequenceTensorLoader,
)

# ---------------------------------------------------------------------------
# SequenceImageSpec
# ---------------------------------------------------------------------------


class TestSequenceImageSpec:
    def test_defaults(self) -> None:
        spec = SequenceImageSpec()
        assert spec.image_name == "trainer-gpu-sequence"
        assert spec.base_image == "pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime"
        assert spec.python_version == "3.12"
        assert spec.gpu_required is True
        assert spec.supports_mixed_precision is True
        assert spec.supports_checkpoint_resume is True

    def test_default_packages(self) -> None:
        spec = SequenceImageSpec()
        assert "torch==2.1.0" in spec.packages
        assert "numpy>=1.26" in spec.packages
        assert "pandas>=2.1" in spec.packages
        assert "scikit-learn>=1.3" in spec.packages
        assert "pydantic>=2.7" in spec.packages
        assert "einops>=0.7" in spec.packages

    def test_healthcheck_cmd_references_sequence_runtime(self) -> None:
        spec = SequenceImageSpec()
        assert "SequenceHealthcheck" in spec.healthcheck_cmd
        assert "sequence_runtime" in spec.healthcheck_cmd

    def test_frozen(self) -> None:
        spec = SequenceImageSpec()
        with pytest.raises(Exception):
            spec.image_name = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            SequenceImageSpec(unexpected="x")  # type: ignore[call-arg]

    def test_custom_construction(self) -> None:
        spec = SequenceImageSpec(
            image_name="custom-sequence",
            base_image="pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime",
            python_version="3.11",
            packages=["torch==2.2.0", "numpy"],
            gpu_required=False,
            supports_mixed_precision=False,
            supports_checkpoint_resume=False,
        )
        assert spec.image_name == "custom-sequence"
        assert spec.gpu_required is False
        assert spec.supports_mixed_precision is False
        assert spec.supports_checkpoint_resume is False


# ---------------------------------------------------------------------------
# CheckpointConfig
# ---------------------------------------------------------------------------


class TestCheckpointConfig:
    def test_defaults(self) -> None:
        cfg = CheckpointConfig(checkpoint_dir="/tmp/ckpt")
        assert cfg.checkpoint_dir == "/tmp/ckpt"
        assert cfg.save_every_n_epochs == 1
        assert cfg.max_checkpoints == 3
        assert cfg.resume_from_checkpoint is None

    def test_frozen(self) -> None:
        cfg = CheckpointConfig(checkpoint_dir="/tmp/ckpt")
        with pytest.raises(Exception):
            cfg.checkpoint_dir = "/other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            CheckpointConfig(checkpoint_dir="/tmp", unexpected=1)  # type: ignore[call-arg]

    def test_save_every_n_epochs_minimum(self) -> None:
        with pytest.raises(Exception):
            CheckpointConfig(checkpoint_dir="/tmp", save_every_n_epochs=0)

    def test_save_every_n_epochs_valid(self) -> None:
        cfg = CheckpointConfig(checkpoint_dir="/tmp", save_every_n_epochs=5)
        assert cfg.save_every_n_epochs == 5

    def test_max_checkpoints_minimum(self) -> None:
        with pytest.raises(Exception):
            CheckpointConfig(checkpoint_dir="/tmp", max_checkpoints=0)

    def test_max_checkpoints_valid(self) -> None:
        cfg = CheckpointConfig(checkpoint_dir="/tmp", max_checkpoints=10)
        assert cfg.max_checkpoints == 10

    def test_resume_from_checkpoint(self) -> None:
        cfg = CheckpointConfig(
            checkpoint_dir="/tmp",
            resume_from_checkpoint="/tmp/ckpt/checkpoint_epoch_3.pt",
        )
        assert cfg.resume_from_checkpoint == "/tmp/ckpt/checkpoint_epoch_3.pt"


# ---------------------------------------------------------------------------
# MixedPrecisionConfig
# ---------------------------------------------------------------------------


class TestMixedPrecisionConfig:
    def test_defaults(self) -> None:
        cfg = MixedPrecisionConfig()
        assert cfg.enabled is False
        assert cfg.dtype == "float16"
        assert cfg.grad_scaler is True

    def test_frozen(self) -> None:
        cfg = MixedPrecisionConfig()
        with pytest.raises(Exception):
            cfg.enabled = True  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            MixedPrecisionConfig(unexpected=1)  # type: ignore[call-arg]

    def test_dtype_float16(self) -> None:
        cfg = MixedPrecisionConfig(enabled=True, dtype="float16")
        assert cfg.dtype == "float16"

    def test_dtype_bfloat16(self) -> None:
        cfg = MixedPrecisionConfig(enabled=True, dtype="bfloat16")
        assert cfg.dtype == "bfloat16"

    def test_dtype_invalid(self) -> None:
        with pytest.raises(Exception):
            MixedPrecisionConfig(dtype="int8")

    def test_dtype_invalid_float32(self) -> None:
        with pytest.raises(Exception):
            MixedPrecisionConfig(dtype="float32")

    def test_grad_scaler_false(self) -> None:
        cfg = MixedPrecisionConfig(enabled=True, grad_scaler=False)
        assert cfg.grad_scaler is False


# ---------------------------------------------------------------------------
# MetricsArtifact
# ---------------------------------------------------------------------------


class TestMetricsArtifact:
    def test_construction(self) -> None:
        m = MetricsArtifact(
            epoch=1,
            train_loss=0.5,
            val_loss=0.6,
            learning_rate=0.001,
            gpu_memory_mb=1024.0,
            epoch_duration_seconds=12.3,
            timestamp="2026-01-01T00:00:00+00:00",
        )
        assert m.epoch == 1
        assert m.train_loss == 0.5
        assert m.val_loss == 0.6
        assert m.learning_rate == 0.001
        assert m.gpu_memory_mb == 1024.0
        assert m.epoch_duration_seconds == 12.3

    def test_optional_fields_none(self) -> None:
        m = MetricsArtifact(
            epoch=0,
            train_loss=1.0,
            learning_rate=0.01,
            epoch_duration_seconds=5.0,
            timestamp="2026-01-01T00:00:00+00:00",
        )
        assert m.val_loss is None
        assert m.gpu_memory_mb is None

    def test_frozen(self) -> None:
        m = MetricsArtifact(
            epoch=0,
            train_loss=1.0,
            learning_rate=0.01,
            epoch_duration_seconds=5.0,
            timestamp="t",
        )
        with pytest.raises(Exception):
            m.epoch = 2  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            MetricsArtifact(  # type: ignore[call-arg]
                epoch=0,
                train_loss=1.0,
                learning_rate=0.01,
                epoch_duration_seconds=5.0,
                timestamp="t",
                extra=1,
            )


# ---------------------------------------------------------------------------
# SequenceTensorLoader (.npz)
# ---------------------------------------------------------------------------


def _write_synthetic_npz(path: Path) -> None:
    """Write a synthetic .npz with sequences + targets arrays."""
    import numpy as np

    sequences = np.random.randn(20, 8, 3).astype(np.float32)
    targets = np.random.randn(20).astype(np.float32)
    np.savez(str(path), sequences=sequences, targets=targets)


class TestSequenceTensorLoaderNpz:
    def test_load_npz(self, tmp_path: Path) -> None:
        npz_path = tmp_path / "data.npz"
        _write_synthetic_npz(npz_path)
        loader = SequenceTensorLoader(str(npz_path))
        data = loader.load()
        assert "sequences" in data
        assert "targets" in data
        assert data["sequences"].shape == (20, 8, 3)
        assert data["targets"].shape == (20,)

    def test_load_caches(self, tmp_path: Path) -> None:
        npz_path = tmp_path / "data.npz"
        _write_synthetic_npz(npz_path)
        loader = SequenceTensorLoader(str(npz_path))
        first = loader.load()
        second = loader.load()
        assert first is second

    def test_validate_shape_match(self, tmp_path: Path) -> None:
        npz_path = tmp_path / "data.npz"
        _write_synthetic_npz(npz_path)
        loader = SequenceTensorLoader(str(npz_path))
        assert loader.validate_shape((20, 8, 3)) is True

    def test_validate_shape_mismatch(self, tmp_path: Path) -> None:
        npz_path = tmp_path / "data.npz"
        _write_synthetic_npz(npz_path)
        loader = SequenceTensorLoader(str(npz_path))
        assert loader.validate_shape((10, 8, 3)) is False

    def test_get_batch(self, tmp_path: Path) -> None:
        npz_path = tmp_path / "data.npz"
        _write_synthetic_npz(npz_path)
        loader = SequenceTensorLoader(str(npz_path))
        batch = loader.get_batch([0, 2, 4])
        assert batch["sequences"].shape == (3, 8, 3)
        assert batch["targets"].shape == (3,)

    def test_get_batch_empty_raises(self, tmp_path: Path) -> None:
        npz_path = tmp_path / "data.npz"
        _write_synthetic_npz(npz_path)
        loader = SequenceTensorLoader(str(npz_path))
        with pytest.raises(ValueError):
            loader.get_batch([])

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        loader = SequenceTensorLoader(str(tmp_path / "nope.npz"))
        with pytest.raises(FileNotFoundError):
            loader.load()

    def test_manifest_data_stored(self, tmp_path: Path) -> None:
        npz_path = tmp_path / "data.npz"
        _write_synthetic_npz(npz_path)
        manifest = {"version": "1.0", "n_samples": 20}
        loader = SequenceTensorLoader(str(npz_path), manifest_data=manifest)
        assert loader.manifest_data == manifest


# ---------------------------------------------------------------------------
# SequenceTensorLoader (sharded parquet)
# ---------------------------------------------------------------------------


def _parquet_available() -> bool:
    """Return True if a parquet engine (pyarrow / fastparquet) is installed."""
    try:
        import pyarrow  # noqa: F401

        return True
    except Exception:
        pass
    try:
        import fastparquet  # noqa: F401

        return True
    except Exception:
        return False


_parquet_skip = pytest.mark.skipif(
    not _parquet_available(),
    reason="no parquet engine (pyarrow / fastparquet) installed",
)


def _write_synthetic_parquet_shards(dir_path: Path) -> None:
    """Write a directory of parquet shards with sequence + target columns."""
    import numpy as np
    import pandas as pd

    dir_path.mkdir(parents=True, exist_ok=True)
    rows = []
    for _i in range(20):
        rows.append(
            {
                "sequence": np.random.randn(8).astype(np.float32).tolist(),
                "target": float(np.random.randn()),
            }
        )
    df = pd.DataFrame(rows)
    # Write two shards.
    df.iloc[:10].to_parquet(str(dir_path / "shard-0.parquet"))
    df.iloc[10:].to_parquet(str(dir_path / "shard-1.parquet"))


class TestSequenceTensorLoaderParquet:
    @_parquet_skip
    def test_load_parquet_shards(self, tmp_path: Path) -> None:
        shard_dir = tmp_path / "shards"
        _write_synthetic_parquet_shards(shard_dir)
        loader = SequenceTensorLoader(str(shard_dir))
        data = loader.load()
        assert "sequences" in data
        assert "targets" in data
        assert data["sequences"].shape == (20, 8)
        assert data["targets"].shape == (20,)

    @_parquet_skip
    def test_validate_shape_parquet(self, tmp_path: Path) -> None:
        shard_dir = tmp_path / "shards"
        _write_synthetic_parquet_shards(shard_dir)
        loader = SequenceTensorLoader(str(shard_dir))
        assert loader.validate_shape((20, 8)) is True

    @_parquet_skip
    def test_get_batch_parquet(self, tmp_path: Path) -> None:
        shard_dir = tmp_path / "shards"
        _write_synthetic_parquet_shards(shard_dir)
        loader = SequenceTensorLoader(str(shard_dir))
        batch = loader.get_batch([1, 5, 15])
        assert batch["sequences"].shape == (3, 8)
        assert batch["targets"].shape == (3,)

    def test_empty_parquet_dir_raises(self, tmp_path: Path) -> None:
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        loader = SequenceTensorLoader(str(empty_dir))
        with pytest.raises(ValueError):
            loader.load()

    def test_unsupported_format_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "data.txt"
        bad.write_text("not a tensor")
        loader = SequenceTensorLoader(str(bad))
        with pytest.raises(ValueError):
            loader.load()


# ---------------------------------------------------------------------------
# CheckpointManager
# ---------------------------------------------------------------------------


class TestCheckpointManager:
    def test_save_and_load(self, tmp_path: Path) -> None:
        cfg = CheckpointConfig(checkpoint_dir=str(tmp_path / "ckpt"))
        mgr = CheckpointManager(cfg)
        model_state = {"layer.weight": "fake"}
        optimizer_state = {"state": {}}
        metrics = {"loss": 0.5}
        path = mgr.save(model_state, optimizer_state, epoch=0, metrics=metrics)
        assert os.path.exists(path)
        payload = mgr.load(path)
        assert payload["model_state"] == model_state
        assert payload["optimizer_state"] == optimizer_state
        assert payload["epoch"] == 0
        assert payload["metrics"] == metrics
        assert "timestamp" in payload

    def test_save_creates_dir(self, tmp_path: Path) -> None:
        cfg = CheckpointConfig(checkpoint_dir=str(tmp_path / "new" / "ckpt"))
        mgr = CheckpointManager(cfg)
        path = mgr.save({}, {}, epoch=0, metrics={})
        assert os.path.exists(path)

    def test_latest_checkpoint_none(self, tmp_path: Path) -> None:
        cfg = CheckpointConfig(checkpoint_dir=str(tmp_path / "ckpt"))
        mgr = CheckpointManager(cfg)
        assert mgr.latest_checkpoint() is None

    def test_latest_checkpoint_returns_highest_epoch(self, tmp_path: Path) -> None:
        cfg = CheckpointConfig(checkpoint_dir=str(tmp_path / "ckpt"))
        mgr = CheckpointManager(cfg)
        mgr.save({}, {}, epoch=0, metrics={})
        mgr.save({}, {}, epoch=2, metrics={})
        mgr.save({}, {}, epoch=1, metrics={})
        latest = mgr.latest_checkpoint()
        assert latest is not None
        assert "checkpoint_epoch_2" in latest

    def test_cleanup_removes_old(self, tmp_path: Path) -> None:
        cfg = CheckpointConfig(checkpoint_dir=str(tmp_path / "ckpt"), max_checkpoints=2)
        mgr = CheckpointManager(cfg)
        for epoch in range(5):
            mgr.save({}, {}, epoch=epoch, metrics={})
        mgr.cleanup()
        remaining = list((tmp_path / "ckpt").glob("checkpoint_epoch_*.pt"))
        assert len(remaining) == 2
        # Should keep epochs 3 and 4.
        epochs = sorted(int(f.stem.replace("checkpoint_epoch_", "")) for f in remaining)
        assert epochs == [3, 4]

    def test_cleanup_noop_when_under_max(self, tmp_path: Path) -> None:
        cfg = CheckpointConfig(checkpoint_dir=str(tmp_path / "ckpt"), max_checkpoints=5)
        mgr = CheckpointManager(cfg)
        mgr.save({}, {}, epoch=0, metrics={})
        mgr.save({}, {}, epoch=1, metrics={})
        mgr.cleanup()
        remaining = list((tmp_path / "ckpt").glob("checkpoint_epoch_*.pt"))
        assert len(remaining) == 2

    def test_load_missing_raises(self, tmp_path: Path) -> None:
        cfg = CheckpointConfig(checkpoint_dir=str(tmp_path / "ckpt"))
        mgr = CheckpointManager(cfg)
        with pytest.raises(FileNotFoundError):
            mgr.load(str(tmp_path / "nope.pt"))

    def test_resume_from_checkpoint_stored(self, tmp_path: Path) -> None:
        cfg = CheckpointConfig(
            checkpoint_dir=str(tmp_path / "ckpt"),
            resume_from_checkpoint="/some/path.pt",
        )
        mgr = CheckpointManager(cfg)
        assert mgr.config.resume_from_checkpoint == "/some/path.pt"


# ---------------------------------------------------------------------------
# MixedPrecisionManager
# ---------------------------------------------------------------------------


class TestMixedPrecisionManager:
    def test_disabled_autocast_is_nullcontext(self) -> None:
        cfg = MixedPrecisionConfig(enabled=False)
        mgr = MixedPrecisionManager(cfg)
        ctx = mgr.autocast_context()
        # nullcontext enters/exits without error.
        with ctx:
            pass

    def test_disabled_scale_loss_returns_loss(self) -> None:
        cfg = MixedPrecisionConfig(enabled=False)
        mgr = MixedPrecisionManager(cfg)
        loss = 1.5
        assert mgr.scale_loss(loss) is loss

    def test_disabled_step_optimizer_calls_step(self) -> None:
        cfg = MixedPrecisionConfig(enabled=False)
        mgr = MixedPrecisionManager(cfg)

        class FakeOpt:
            def __init__(self) -> None:
                self.stepped = False

            def step(self) -> None:
                self.stepped = True

        opt = FakeOpt()
        mgr.step_optimizer(opt)
        assert opt.stepped is True

    def test_disabled_update_is_noop(self) -> None:
        cfg = MixedPrecisionConfig(enabled=False)
        mgr = MixedPrecisionManager(cfg)
        mgr.update()  # should not raise

    def test_enabled_bfloat16_autocast_cpu(self) -> None:
        # On a CPU-only host, bfloat16 autocast is supported on CPU.
        cfg = MixedPrecisionConfig(enabled=True, dtype="bfloat16")
        mgr = MixedPrecisionManager(cfg)
        import torch

        with mgr.autocast_context():
            x = torch.randn(2, 3)
            y = x + 1
        assert y.shape == (2, 3)

    def test_enabled_float16_scale_loss_on_cpu(self) -> None:
        # float16 on CPU: scaler is not built (no CUDA), so scale_loss
        # returns the loss unchanged.
        cfg = MixedPrecisionConfig(enabled=True, dtype="float16")
        mgr = MixedPrecisionManager(cfg)
        import torch

        loss = torch.tensor(1.0)
        scaled = mgr.scale_loss(loss)
        # On CPU, no scaler -> returns loss unchanged.
        assert scaled is loss

    def test_enabled_float16_step_optimizer_no_scaler_cpu(self) -> None:
        cfg = MixedPrecisionConfig(enabled=True, dtype="float16")
        mgr = MixedPrecisionManager(cfg)
        import torch

        model = torch.nn.Linear(2, 1)
        opt = torch.optim.SGD(model.parameters(), lr=0.01)
        # Should not raise even without a real scaler on CPU.
        mgr.step_optimizer(opt)
        mgr.update()


# ---------------------------------------------------------------------------
# SequenceHealthcheck
# ---------------------------------------------------------------------------


class TestSequenceHealthcheck:
    def test_init_default_timeout(self) -> None:
        hc = SequenceHealthcheck()
        assert hc.timeout_seconds == 60

    def test_init_custom_timeout(self) -> None:
        hc = SequenceHealthcheck(timeout_seconds=30)
        assert hc.timeout_seconds == 30

    def test_init_invalid_timeout(self) -> None:
        with pytest.raises(ValueError):
            SequenceHealthcheck(timeout_seconds=0)
        with pytest.raises(ValueError):
            SequenceHealthcheck(timeout_seconds=-1)

    def test_run_returns_status_dict(self) -> None:
        hc = SequenceHealthcheck()
        status = hc.run()
        assert "healthy" in status
        assert "gpu" in status
        assert "tensor_load" in status
        assert "forward_pass" in status
        assert "error" in status
        assert "duration_seconds" in status

    def test_run_tensor_load_and_forward_pass_succeed(self) -> None:
        hc = SequenceHealthcheck()
        status = hc.run()
        # On CPU, tensor load + forward pass should succeed (torch installed).
        assert status["tensor_load"] is True
        assert status["forward_pass"] is True
        assert status["error"] is None

    def test_run_unhealthy_on_cpu(self) -> None:
        # On a CPU-only host, GPU is not available -> unhealthy.
        hc = SequenceHealthcheck()
        status = hc.run()
        if status["gpu"] and not status["gpu"]["available"]:
            assert status["healthy"] is False
            assert hc.is_healthy() is False

    def test_is_healthy_returns_bool(self) -> None:
        hc = SequenceHealthcheck()
        assert isinstance(hc.is_healthy(), bool)

    def test_run_duration_positive(self) -> None:
        hc = SequenceHealthcheck()
        status = hc.run()
        assert status["duration_seconds"] >= 0.0
