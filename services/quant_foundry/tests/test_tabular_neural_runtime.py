"""Tests for quant_foundry.tabular_neural_runtime (T-9.1).

Covers the PyTorch tabular neural runtime: telemetry models, GPU probe
helpers, the TinyTabularNet MLP, the canary training loop, artifact
round-trip, the healthcheck, and the Docker ImageSpec.

The test host is CPU-only (torch is installed with the CPU index URL), so
GPU-dependent assertions check the "no GPU" degradation path. The canary
training + forward pass + artifact round-trip run on CPU.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("torch")

from quant_foundry.tabular_neural_runtime import (
    GPUStatus,
    GPUMemorySnapshot,
    ImageSpec,
    NeuralCanaryConfig,
    NeuralCanaryResult,
    TabularNeuralHealthcheck,
    TinyTabularNet,
    check_gpu,
    get_gpu_memory_snapshot,
    load_neural_artifact,
    run_neural_canary,
    save_neural_artifact,
)


# ---------------------------------------------------------------------------
# GPUStatus
# ---------------------------------------------------------------------------


class TestGPUStatus:
    def test_available_false_defaults(self) -> None:
        status = GPUStatus(available=False)
        assert status.available is False
        assert status.device_name is None
        assert status.cuda_version is None
        assert status.memory_total_mb is None
        assert status.memory_free_mb is None
        assert status.memory_used_mb is None

    def test_frozen(self) -> None:
        status = GPUStatus(available=False)
        with pytest.raises(Exception):
            status.available = True  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            GPUStatus(available=False, unexpected="x")  # type: ignore[call-arg]

    def test_full_construction(self) -> None:
        status = GPUStatus(
            available=True,
            device_name="NVIDIA RTX 4090",
            cuda_version="12.1",
            memory_total_mb=24576.0,
            memory_free_mb=12000.0,
            memory_used_mb=12576.0,
        )
        assert status.available is True
        assert status.device_name == "NVIDIA RTX 4090"
        assert status.memory_total_mb == 24576.0


# ---------------------------------------------------------------------------
# GPUMemorySnapshot
# ---------------------------------------------------------------------------


class TestGPUMemorySnapshot:
    def test_construction(self) -> None:
        snap = GPUMemorySnapshot(
            allocated_mb=10.0,
            reserved_mb=20.0,
            free_mb=8000.0,
            timestamp="2026-01-01T00:00:00+00:00",
        )
        assert snap.allocated_mb == 10.0
        assert snap.reserved_mb == 20.0
        assert snap.free_mb == 8000.0
        assert snap.timestamp == "2026-01-01T00:00:00+00:00"

    def test_free_optional(self) -> None:
        snap = GPUMemorySnapshot(
            allocated_mb=0.0,
            reserved_mb=0.0,
            free_mb=None,
            timestamp="2026-01-01T00:00:00+00:00",
        )
        assert snap.free_mb is None

    def test_frozen(self) -> None:
        snap = GPUMemorySnapshot(
            allocated_mb=0.0, reserved_mb=0.0, timestamp="t"
        )
        with pytest.raises(Exception):
            snap.allocated_mb = 1.0  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            GPUMemorySnapshot(  # type: ignore[call-arg]
                allocated_mb=0.0, reserved_mb=0.0, timestamp="t", extra=1
            )


# ---------------------------------------------------------------------------
# NeuralCanaryConfig
# ---------------------------------------------------------------------------


class TestNeuralCanaryConfig:
    def test_defaults(self) -> None:
        cfg = NeuralCanaryConfig()
        assert cfg.input_dim == 10
        assert cfg.hidden_dims == [32, 16]
        assert cfg.output_dim == 1
        assert cfg.learning_rate == 0.001
        assert cfg.epochs == 5
        assert cfg.batch_size == 32
        assert cfg.device == "auto"
        assert cfg.seed == 42

    def test_custom(self) -> None:
        cfg = NeuralCanaryConfig(
            input_dim=20, hidden_dims=[64, 32, 16], output_dim=3, epochs=2
        )
        assert cfg.input_dim == 20
        assert cfg.hidden_dims == [64, 32, 16]
        assert cfg.output_dim == 3
        assert cfg.epochs == 2

    def test_frozen(self) -> None:
        cfg = NeuralCanaryConfig()
        with pytest.raises(Exception):
            cfg.epochs = 99  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            NeuralCanaryConfig(foo=1)  # type: ignore[call-arg]

    def test_hidden_dims_default_is_new_list(self) -> None:
        # default_factory must produce a fresh list per instance
        a = NeuralCanaryConfig()
        b = NeuralCanaryConfig()
        a.hidden_dims.append(8)
        assert b.hidden_dims == [32, 16]


# ---------------------------------------------------------------------------
# NeuralCanaryResult
# ---------------------------------------------------------------------------


class TestNeuralCanaryResult:
    def test_construction(self) -> None:
        cfg = NeuralCanaryConfig()
        result = NeuralCanaryResult(
            config=cfg,
            final_loss=0.5,
            gpu_status=GPUStatus(available=False),
            memory_snapshots=[],
            artifact_path=None,
            trained=True,
            duration_seconds=0.1,
        )
        assert result.config == cfg
        assert result.final_loss == 0.5
        assert result.trained is True
        assert result.artifact_path is None

    def test_frozen(self) -> None:
        result = NeuralCanaryResult(
            config=NeuralCanaryConfig(),
            final_loss=0.5,
            gpu_status=GPUStatus(available=False),
            memory_snapshots=[],
            trained=True,
            duration_seconds=0.1,
        )
        with pytest.raises(Exception):
            result.final_loss = 0.1  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            NeuralCanaryResult(  # type: ignore[call-arg]
                config=NeuralCanaryConfig(),
                final_loss=0.5,
                gpu_status=GPUStatus(available=False),
                memory_snapshots=[],
                trained=True,
                duration_seconds=0.1,
                extra=1,
            )


# ---------------------------------------------------------------------------
# check_gpu / get_gpu_memory_snapshot (CPU-only host)
# ---------------------------------------------------------------------------


class TestGPUProbe:
    def test_check_gpu_returns_status(self) -> None:
        status = check_gpu()
        assert isinstance(status, GPUStatus)

    def test_check_gpu_no_gpu_on_cpu_host(self) -> None:
        status = check_gpu()
        # The test host is CPU-only; CUDA should not be available.
        if not _has_cuda():
            assert status.available is False
            assert status.device_name is None

    def test_get_gpu_memory_snapshot_returns_snapshot(self) -> None:
        snap = get_gpu_memory_snapshot()
        assert isinstance(snap, GPUMemorySnapshot)
        assert snap.allocated_mb >= 0.0
        assert snap.reserved_mb >= 0.0

    def test_get_gpu_memory_snapshot_timestamp_iso(self) -> None:
        snap = get_gpu_memory_snapshot()
        # ISO-8601 string with timezone offset.
        assert "T" in snap.timestamp
        assert snap.timestamp.endswith("+00:00") or snap.timestamp.endswith("Z") or "+" in snap.timestamp

    def test_get_gpu_memory_snapshot_no_gpu_zeros(self) -> None:
        snap = get_gpu_memory_snapshot()
        if not _has_cuda():
            assert snap.allocated_mb == 0.0
            assert snap.reserved_mb == 0.0
            assert snap.free_mb is None


def _has_cuda() -> bool:
    """Return True if torch + CUDA are available in the test env."""
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# TinyTabularNet
# ---------------------------------------------------------------------------


class TestTinyTabularNet:
    def test_build_and_forward(self) -> None:
        import torch

        net = TinyTabularNet(
            input_dim=10, hidden_dims=[32, 16], output_dim=1
        )
        net.eval()
        x = torch.randn(8, 10)
        out = net.forward(x)
        assert out.shape == (8, 1)

    def test_forward_multi_output(self) -> None:
        import torch

        net = TinyTabularNet(
            input_dim=5, hidden_dims=[16], output_dim=3
        )
        net.eval()
        x = torch.randn(4, 5)
        out = net.forward(x)
        assert out.shape == (4, 3)

    def test_binary_sigmoid_output(self) -> None:
        import torch

        net = TinyTabularNet(
            input_dim=4, hidden_dims=[8], output_dim=1, binary=True
        )
        net.eval()
        x = torch.randn(16, 4)
        out = net.forward(x)
        # Sigmoid output is in [0, 1].
        assert bool((out >= 0.0).all())
        assert bool((out <= 1.0).all())

    def test_invalid_input_dim(self) -> None:
        with pytest.raises(ValueError):
            TinyTabularNet(input_dim=0, hidden_dims=[8])

    def test_invalid_output_dim(self) -> None:
        with pytest.raises(ValueError):
            TinyTabularNet(input_dim=4, hidden_dims=[8], output_dim=0)

    def test_empty_hidden_dims(self) -> None:
        with pytest.raises(ValueError):
            TinyTabularNet(input_dim=4, hidden_dims=[])

    def test_negative_hidden_dim(self) -> None:
        with pytest.raises(ValueError):
            TinyTabularNet(input_dim=4, hidden_dims=[-1])

    def test_invalid_dropout(self) -> None:
        with pytest.raises(ValueError):
            TinyTabularNet(input_dim=4, hidden_dims=[8], dropout=1.5)

    def test_state_dict_roundtrip_in_memory(self) -> None:
        import torch

        net = TinyTabularNet(input_dim=6, hidden_dims=[12], output_dim=2)
        net.eval()
        x = torch.randn(4, 6)
        out_before = net.forward(x).detach().clone()

        sd = net.state_dict()
        net2 = TinyTabularNet(input_dim=6, hidden_dims=[12], output_dim=2)
        _ = net2.module  # build
        net2.load_state_dict(sd)
        net2.eval()
        out_after = net2.forward(x).detach()
        assert torch.allclose(out_before, out_after)

    def test_module_cached(self) -> None:
        net = TinyTabularNet(input_dim=4, hidden_dims=[8])
        m1 = net.module
        m2 = net.module
        assert m1 is m2


# ---------------------------------------------------------------------------
# run_neural_canary
# ---------------------------------------------------------------------------


class TestRunNeuralCanary:
    def test_canary_cpu(self) -> None:
        cfg = NeuralCanaryConfig(
            input_dim=8,
            hidden_dims=[16, 8],
            output_dim=1,
            epochs=2,
            batch_size=16,
            device="cpu",
        )
        result = run_neural_canary(cfg)
        assert isinstance(result, NeuralCanaryResult)
        assert result.config == cfg
        assert result.trained is True
        assert result.final_loss > 0
        assert result.duration_seconds >= 0.0
        # Initial snapshot + one per epoch.
        assert len(result.memory_snapshots) == cfg.epochs + 1

    def test_canary_auto_device(self) -> None:
        cfg = NeuralCanaryConfig(
            input_dim=4, hidden_dims=[8], epochs=1, batch_size=8, device="auto"
        )
        result = run_neural_canary(cfg)
        assert result.trained is True
        assert len(result.memory_snapshots) == 2

    def test_canary_zero_epochs(self) -> None:
        cfg = NeuralCanaryConfig(
            input_dim=4, hidden_dims=[8], epochs=0, batch_size=8, device="cpu"
        )
        result = run_neural_canary(cfg)
        assert result.trained is False
        # initial + final snapshot
        assert len(result.memory_snapshots) == 2

    def test_canary_saves_artifact(self, tmp_path: Path) -> None:
        cfg = NeuralCanaryConfig(
            input_dim=4, hidden_dims=[8], epochs=1, batch_size=8, device="cpu"
        )
        artifact = tmp_path / "model.pt"
        result = run_neural_canary(cfg, artifact_path=str(artifact))
        assert result.artifact_path == str(artifact)
        assert artifact.exists()

    def test_canary_creates_parent_dirs(self, tmp_path: Path) -> None:
        cfg = NeuralCanaryConfig(
            input_dim=4, hidden_dims=[8], epochs=1, batch_size=8, device="cpu"
        )
        artifact = tmp_path / "nested" / "deep" / "model.pt"
        result = run_neural_canary(cfg, artifact_path=str(artifact))
        assert result.artifact_path == str(artifact)
        assert artifact.exists()

    def test_canary_no_artifact_when_path_none(self) -> None:
        cfg = NeuralCanaryConfig(
            input_dim=4, hidden_dims=[8], epochs=1, batch_size=8, device="cpu"
        )
        result = run_neural_canary(cfg, artifact_path=None)
        assert result.artifact_path is None

    def test_canary_invalid_epochs(self) -> None:
        cfg = NeuralCanaryConfig(
            input_dim=4, hidden_dims=[8], epochs=-1, device="cpu"
        )
        with pytest.raises(ValueError):
            run_neural_canary(cfg)

    def test_canary_invalid_batch_size(self) -> None:
        cfg = NeuralCanaryConfig(
            input_dim=4, hidden_dims=[8], epochs=1, batch_size=0, device="cpu"
        )
        with pytest.raises(ValueError):
            run_neural_canary(cfg)

    def test_canary_invalid_learning_rate(self) -> None:
        cfg = NeuralCanaryConfig(
            input_dim=4, hidden_dims=[8], epochs=1, learning_rate=0.0, device="cpu"
        )
        with pytest.raises(ValueError):
            run_neural_canary(cfg)

    def test_canary_gpu_status_present(self) -> None:
        cfg = NeuralCanaryConfig(
            input_dim=4, hidden_dims=[8], epochs=1, device="cpu"
        )
        result = run_neural_canary(cfg)
        assert isinstance(result.gpu_status, GPUStatus)

    def test_canary_memory_snapshots_typed(self) -> None:
        cfg = NeuralCanaryConfig(
            input_dim=4, hidden_dims=[8], epochs=2, device="cpu"
        )
        result = run_neural_canary(cfg)
        for snap in result.memory_snapshots:
            assert isinstance(snap, GPUMemorySnapshot)


# ---------------------------------------------------------------------------
# save / load artifact
# ---------------------------------------------------------------------------


class TestArtifactPersistence:
    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        import torch

        cfg = NeuralCanaryConfig(
            input_dim=6, hidden_dims=[12], output_dim=2, device="cpu"
        )
        net = TinyTabularNet(
            input_dim=6, hidden_dims=[12], output_dim=2
        )
        net.eval()
        save_neural_artifact(net, str(tmp_path / "m.pt"))

        loaded = load_neural_artifact(str(tmp_path / "m.pt"), cfg)
        loaded.eval()
        x = torch.randn(4, 6)
        out1 = net.forward(x).detach()
        out2 = loaded.forward(x).detach()
        assert torch.allclose(out1, out2)

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        net = TinyTabularNet(input_dim=4, hidden_dims=[8])
        path = tmp_path / "a" / "b" / "m.pt"
        save_neural_artifact(net, str(path))
        assert path.exists()

    def test_load_returns_eval_mode(self, tmp_path: Path) -> None:
        cfg = NeuralCanaryConfig(input_dim=4, hidden_dims=[8], device="cpu")
        net = TinyTabularNet(input_dim=4, hidden_dims=[8])
        save_neural_artifact(net, str(tmp_path / "m.pt"))
        loaded = load_neural_artifact(str(tmp_path / "m.pt"), cfg)
        # eval mode -> the underlying module's training flag is False.
        assert loaded.module.training is False

    def test_load_missing_file_raises(self, tmp_path: Path) -> None:
        cfg = NeuralCanaryConfig(input_dim=4, hidden_dims=[8])
        with pytest.raises(Exception):
            load_neural_artifact(str(tmp_path / "nope.pt"), cfg)

    def test_save_load_via_canary_roundtrip(self, tmp_path: Path) -> None:
        import torch

        cfg = NeuralCanaryConfig(
            input_dim=5, hidden_dims=[10], epochs=1, batch_size=8, device="cpu"
        )
        path = tmp_path / "canary.pt"
        result = run_neural_canary(cfg, artifact_path=str(path))
        assert path.exists()

        loaded = load_neural_artifact(str(path), cfg)
        loaded.eval()
        x = torch.randn(4, 5)
        out = loaded.forward(x).detach()
        assert out.shape == (4, 1)
        # The result should reflect the saved artifact path.
        assert result.artifact_path == str(path)


# ---------------------------------------------------------------------------
# TabularNeuralHealthcheck
# ---------------------------------------------------------------------------


class TestTabularNeuralHealthcheck:
    def test_run_returns_dict(self) -> None:
        hc = TabularNeuralHealthcheck()
        status = hc.run()
        assert isinstance(status, dict)
        assert "healthy" in status
        assert "gpu" in status
        assert "canary" in status
        assert "error" in status
        assert "duration_seconds" in status

    def test_run_gpu_field_is_dict(self) -> None:
        hc = TabularNeuralHealthcheck()
        status = hc.run()
        assert isinstance(status["gpu"], dict)
        assert "available" in status["gpu"]

    def test_run_canary_field_on_cpu(self) -> None:
        hc = TabularNeuralHealthcheck()
        status = hc.run()
        # On CPU the canary should still train (trained=True), but
        # healthy=False because no GPU.
        if status["canary"] is not None:
            assert status["canary"]["trained"] is True

    def test_is_healthy_returns_bool(self) -> None:
        hc = TabularNeuralHealthcheck()
        assert isinstance(hc.is_healthy(), bool)

    def test_is_healthy_false_on_cpu_host(self) -> None:
        if _has_cuda():
            pytest.skip("GPU host; is_healthy may be True")
        hc = TabularNeuralHealthcheck()
        assert hc.is_healthy() is False

    def test_invalid_timeout(self) -> None:
        with pytest.raises(ValueError):
            TabularNeuralHealthcheck(timeout_seconds=0)

    def test_negative_timeout(self) -> None:
        with pytest.raises(ValueError):
            TabularNeuralHealthcheck(timeout_seconds=-5)

    def test_run_duration_nonnegative(self) -> None:
        hc = TabularNeuralHealthcheck()
        status = hc.run()
        assert status["duration_seconds"] >= 0.0

    def test_run_no_error_on_cpu(self) -> None:
        hc = TabularNeuralHealthcheck()
        status = hc.run()
        # The canary should succeed on CPU; error should be None.
        assert status["error"] is None


# ---------------------------------------------------------------------------
# ImageSpec
# ---------------------------------------------------------------------------


class TestImageSpec:
    def test_defaults(self) -> None:
        spec = ImageSpec()
        assert spec.image_name == "trainer-gpu-tabular-neural"
        assert spec.base_image == "pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime"
        assert spec.python_version == "3.12"
        assert spec.gpu_required is True
        pkgs = " ".join(spec.packages)
        assert "torch" in pkgs
        assert "numpy" in pkgs
        assert "pandas" in pkgs
        assert "scikit-learn" in pkgs
        assert "pydantic" in pkgs
        assert "TabularNeuralHealthcheck" in spec.healthcheck_cmd

    def test_frozen(self) -> None:
        spec = ImageSpec()
        with pytest.raises(Exception):
            spec.image_name = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            ImageSpec(foo=1)  # type: ignore[call-arg]

    def test_custom_packages(self) -> None:
        spec = ImageSpec(packages=["torch==2.1.0", "numpy"])
        assert spec.packages == ["torch==2.1.0", "numpy"]

    def test_custom_base_image(self) -> None:
        spec = ImageSpec(base_image="pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime")
        assert spec.base_image == "pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime"

    def test_gpu_required_can_be_false(self) -> None:
        spec = ImageSpec(gpu_required=False)
        assert spec.gpu_required is False

    def test_healthcheck_cmd_is_nonempty(self) -> None:
        spec = ImageSpec()
        assert len(spec.healthcheck_cmd) > 0
        assert "python" in spec.healthcheck_cmd

    def test_packages_default_is_new_list(self) -> None:
        a = ImageSpec()
        b = ImageSpec()
        a.packages.append("extra")
        assert "extra" not in b.packages


# ---------------------------------------------------------------------------
# Module importability / lazy import
# ---------------------------------------------------------------------------


class TestModuleImportability:
    def test_models_importable_without_torch_usage(self) -> None:
        # Constructing models must not require torch.
        GPUStatus(available=False)
        GPUMemorySnapshot(allocated_mb=0.0, reserved_mb=0.0, timestamp="t")
        NeuralCanaryConfig()
        ImageSpec()

    def test_tiny_tabular_net_validation_without_torch(self) -> None:
        # Validation errors are raised before torch is imported.
        with pytest.raises(ValueError):
            TinyTabularNet(input_dim=0, hidden_dims=[8])
