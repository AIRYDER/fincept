"""Tests for quant_foundry.foundation_runtime (T-11.4).

Covers the foundation TS runtime: the Docker image spec, batch forecast
config, batch forecast result, the foundation forecast adapter, the
weight cache manager, and the foundation healthcheck.

The test host is CPU-only (torch is installed with the CPU index URL), so
GPU-dependent assertions check the "no GPU" degradation path. The weight
cache manager tests use synthetic temp directories. The forecast adapter
tests use a synthetic weight file and the self-contained projection path
(no real transformers checkpoint is downloaded).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytest.importorskip("torch")

from quant_foundry.foundation_runtime import (
    BatchForecastConfig,
    BatchForecastResult,
    FoundationForecastAdapter,
    FoundationHealthcheck,
    FoundationImageSpec,
    WeightCacheManager,
)
from quant_foundry.tabular_neural_runtime import GPUStatus

# ---------------------------------------------------------------------------
# FoundationImageSpec
# ---------------------------------------------------------------------------


class TestFoundationImageSpec:
    def test_defaults(self) -> None:
        spec = FoundationImageSpec()
        assert spec.image_name == "trainer-gpu-foundation-ts"
        assert spec.base_image == "pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime"
        assert spec.python_version == "3.12"
        assert spec.gpu_required is True
        assert spec.offline_mode is True
        assert spec.supports_batch_forecast is True
        assert spec.weight_cache_dir == "/opt/foundation_weights"

    def test_default_packages(self) -> None:
        spec = FoundationImageSpec()
        assert "torch==2.1.0" in spec.packages
        assert "numpy>=1.26" in spec.packages
        assert "pandas>=2.1" in spec.packages
        assert "pydantic>=2.7" in spec.packages
        assert "transformers>=4.36" in spec.packages
        assert "huggingface_hub>=0.20" in spec.packages

    def test_healthcheck_cmd_references_foundation_runtime(self) -> None:
        spec = FoundationImageSpec()
        assert "FoundationHealthcheck" in spec.healthcheck_cmd
        assert "foundation_runtime" in spec.healthcheck_cmd

    def test_frozen(self) -> None:
        spec = FoundationImageSpec()
        with pytest.raises(Exception):
            spec.image_name = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            FoundationImageSpec(unexpected="x")  # type: ignore[call-arg]

    def test_custom_construction(self) -> None:
        spec = FoundationImageSpec(
            image_name="custom-foundation",
            base_image="pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime",
            python_version="3.11",
            packages=["torch==2.2.0", "numpy"],
            gpu_required=False,
            offline_mode=False,
            supports_batch_forecast=False,
            weight_cache_dir="/data/weights",
        )
        assert spec.image_name == "custom-foundation"
        assert spec.gpu_required is False
        assert spec.offline_mode is False
        assert spec.supports_batch_forecast is False
        assert spec.weight_cache_dir == "/data/weights"


# ---------------------------------------------------------------------------
# BatchForecastConfig
# ---------------------------------------------------------------------------


class TestBatchForecastConfig:
    def test_defaults(self) -> None:
        cfg = BatchForecastConfig(model_id="test-model")
        assert cfg.model_id == "test-model"
        assert cfg.batch_size == 32
        assert cfg.context_length == 512
        assert cfg.prediction_length == 24
        assert cfg.device == "auto"
        assert cfg.num_samples == 100
        assert cfg.seed == 42

    def test_frozen(self) -> None:
        cfg = BatchForecastConfig(model_id="m")
        with pytest.raises(Exception):
            cfg.model_id = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            BatchForecastConfig(model_id="m", unexpected=1)  # type: ignore[call-arg]

    def test_batch_size_minimum(self) -> None:
        with pytest.raises(Exception):
            BatchForecastConfig(model_id="m", batch_size=0)

    def test_batch_size_valid(self) -> None:
        cfg = BatchForecastConfig(model_id="m", batch_size=1)
        assert cfg.batch_size == 1

    def test_context_length_minimum(self) -> None:
        with pytest.raises(Exception):
            BatchForecastConfig(model_id="m", context_length=0)

    def test_context_length_valid(self) -> None:
        cfg = BatchForecastConfig(model_id="m", context_length=1)
        assert cfg.context_length == 1

    def test_prediction_length_minimum(self) -> None:
        with pytest.raises(Exception):
            BatchForecastConfig(model_id="m", prediction_length=0)

    def test_prediction_length_valid(self) -> None:
        cfg = BatchForecastConfig(model_id="m", prediction_length=1)
        assert cfg.prediction_length == 1

    def test_num_samples_minimum(self) -> None:
        with pytest.raises(Exception):
            BatchForecastConfig(model_id="m", num_samples=0)

    def test_num_samples_valid(self) -> None:
        cfg = BatchForecastConfig(model_id="m", num_samples=1)
        assert cfg.num_samples == 1

    def test_device_invalid(self) -> None:
        with pytest.raises(Exception):
            BatchForecastConfig(model_id="m", device="tpu")

    def test_device_valid_cpu(self) -> None:
        cfg = BatchForecastConfig(model_id="m", device="cpu")
        assert cfg.device == "cpu"

    def test_device_valid_cuda(self) -> None:
        cfg = BatchForecastConfig(model_id="m", device="cuda")
        assert cfg.device == "cuda"

    def test_model_id_empty(self) -> None:
        with pytest.raises(Exception):
            BatchForecastConfig(model_id="")

    def test_model_id_whitespace_only(self) -> None:
        with pytest.raises(Exception):
            BatchForecastConfig(model_id="   ")


# ---------------------------------------------------------------------------
# BatchForecastResult
# ---------------------------------------------------------------------------


class TestBatchForecastResult:
    def _make_result(self) -> BatchForecastResult:
        cfg = BatchForecastConfig(model_id="m", batch_size=2, prediction_length=3)
        gpu = GPUStatus(available=False)
        return BatchForecastResult(
            model_id="m",
            config=cfg,
            predictions=[[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]],
            weight_hash="abc123",
            gpu_status=gpu,
            duration_seconds=0.5,
            offline=True,
        )

    def test_construction(self) -> None:
        r = self._make_result()
        assert r.model_id == "m"
        assert len(r.predictions) == 2
        assert r.predictions[0] == [1.0, 2.0, 3.0]
        assert r.weight_hash == "abc123"
        assert r.offline is True
        assert r.duration_seconds == 0.5

    def test_frozen(self) -> None:
        r = self._make_result()
        with pytest.raises(Exception):
            r.model_id = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        cfg = BatchForecastConfig(model_id="m")
        gpu = GPUStatus(available=False)
        with pytest.raises(Exception):
            BatchForecastResult(
                model_id="m",
                config=cfg,
                predictions=[],
                weight_hash="x",
                gpu_status=gpu,
                duration_seconds=0.0,
                offline=True,
                unexpected=1,  # type: ignore[call-arg]
            )

    def test_gpu_status_serialization(self) -> None:
        r = self._make_result()
        dumped = r.model_dump()
        assert dumped["gpu_status"]["available"] is False


# ---------------------------------------------------------------------------
# WeightCacheManager
# ---------------------------------------------------------------------------


class TestWeightCacheManager:
    def test_empty_cache(self, tmp_path: Path) -> None:
        mgr = WeightCacheManager(str(tmp_path / "cache"))
        assert mgr.list_cached_weights() == []
        assert mgr.cache_size_bytes() == 0

    def test_list_cached_weights(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "model_a.bin").write_bytes(b"weights-a")
        (cache / "model_b.pt").write_bytes(b"weights-b")
        (cache / "model_c.safetensors").write_bytes(b"weights-c")
        (cache / "not_a_weight.txt").write_bytes(b"ignore")
        mgr = WeightCacheManager(str(cache))
        ids = mgr.list_cached_weights()
        assert ids == ["model_a", "model_b", "model_c"]

    def test_get_weight_path_found(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "model_a.bin").write_bytes(b"weights-a")
        mgr = WeightCacheManager(str(cache))
        path = mgr.get_weight_path("model_a")
        assert path is not None
        assert path.endswith("model_a.bin")

    def test_get_weight_path_missing(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        mgr = WeightCacheManager(str(cache))
        assert mgr.get_weight_path("nonexistent") is None

    def test_get_weight_path_nonexistent_dir(self, tmp_path: Path) -> None:
        mgr = WeightCacheManager(str(tmp_path / "nope"))
        assert mgr.get_weight_path("model_a") is None

    def test_verify_weight_match(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        data = b"weights-a"
        (cache / "model_a.bin").write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        mgr = WeightCacheManager(str(cache))
        assert mgr.verify_weight("model_a", expected) is True

    def test_verify_weight_mismatch(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "model_a.bin").write_bytes(b"weights-a")
        mgr = WeightCacheManager(str(cache))
        assert mgr.verify_weight("model_a", "0" * 64) is False

    def test_verify_weight_missing(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        mgr = WeightCacheManager(str(cache))
        assert mgr.verify_weight("nonexistent", "0" * 64) is False

    def test_cache_size_bytes(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "model_a.bin").write_bytes(b"12345")
        (cache / "model_b.pt").write_bytes(b"abc")
        mgr = WeightCacheManager(str(cache))
        assert mgr.cache_size_bytes() == 8

    def test_cache_size_bytes_empty(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        mgr = WeightCacheManager(str(cache))
        assert mgr.cache_size_bytes() == 0

    def test_cache_size_bytes_nonexistent_dir(self, tmp_path: Path) -> None:
        mgr = WeightCacheManager(str(tmp_path / "nope"))
        assert mgr.cache_size_bytes() == 0

    def test_hash_file_static(self, tmp_path: Path) -> None:
        f = tmp_path / "w.bin"
        data = b"hello"
        f.write_bytes(data)
        h = WeightCacheManager.hash_file(str(f))
        assert h == hashlib.sha256(data).hexdigest()

    def test_ensure_dir_creates_directory(self, tmp_path: Path) -> None:
        cache = tmp_path / "new_cache"
        mgr = WeightCacheManager(str(cache))
        assert not cache.exists()
        mgr._ensure_dir()
        assert cache.exists()


# ---------------------------------------------------------------------------
# FoundationForecastAdapter
# ---------------------------------------------------------------------------


class TestFoundationForecastAdapter:
    def _make_weight_file(self, tmp_path: Path) -> tuple[str, str]:
        """Create a synthetic weight file and return (path, hash)."""
        import torch  # lazy but torch is available in tests

        tmp_path.mkdir(parents=True, exist_ok=True)
        f = tmp_path / "model_weights.pt"
        torch.save({"layer.weight": torch.zeros(2, 2)}, str(f))
        h = WeightCacheManager.hash_file(str(f))
        return str(f), h

    def test_validate_offline_local_path(self, tmp_path: Path) -> None:
        path, _ = self._make_weight_file(tmp_path)
        cfg = BatchForecastConfig(model_id="m")
        adapter = FoundationForecastAdapter(config=cfg, weight_path=path, weight_hash="x")
        assert adapter.validate_offline() is True

    def test_validate_offline_nonexistent_with_suffix(self, tmp_path: Path) -> None:
        cfg = BatchForecastConfig(model_id="m")
        adapter = FoundationForecastAdapter(
            config=cfg,
            weight_path=str(tmp_path / "missing.bin"),
            weight_hash="x",
        )
        # A path with a suffix is treated as a local (offline) path.
        assert adapter.validate_offline() is True

    def test_validate_offline_hub_id_no_suffix(self, tmp_path: Path) -> None:
        cfg = BatchForecastConfig(model_id="m")
        adapter = FoundationForecastAdapter(
            config=cfg, weight_path="bert-base-uncased", weight_hash="x"
        )
        # A bare Hub repo ID (no suffix, not a local file) is not offline.
        assert adapter.validate_offline() is False

    def test_forecast_returns_result(self, tmp_path: Path) -> None:
        path, whash = self._make_weight_file(tmp_path)
        cfg = BatchForecastConfig(
            model_id="test-model",
            batch_size=4,
            context_length=8,
            prediction_length=5,
            num_samples=1,
            device="cpu",
        )
        adapter = FoundationForecastAdapter(config=cfg, weight_path=path, weight_hash=whash)
        import torch

        ctx = torch.randn(4, 8)
        result = adapter.forecast(ctx)
        assert isinstance(result, BatchForecastResult)
        assert result.model_id == "test-model"
        assert len(result.predictions) == 4
        assert all(len(row) == 5 for row in result.predictions)
        assert result.weight_hash == whash
        assert result.offline is True
        assert result.duration_seconds >= 0.0

    def test_forecast_reproducible_with_seed(self, tmp_path: Path) -> None:
        path, whash = self._make_weight_file(tmp_path)
        cfg = BatchForecastConfig(
            model_id="m",
            batch_size=2,
            context_length=4,
            prediction_length=3,
            num_samples=1,
            seed=123,
            device="cpu",
        )
        adapter1 = FoundationForecastAdapter(config=cfg, weight_path=path, weight_hash=whash)
        adapter2 = FoundationForecastAdapter(config=cfg, weight_path=path, weight_hash=whash)
        import torch

        ctx = torch.randn(2, 4)
        r1 = adapter1.forecast(ctx)
        r2 = adapter2.forecast(ctx)
        assert r1.predictions == r2.predictions

    def test_forecast_tiles_small_batch(self, tmp_path: Path) -> None:
        path, whash = self._make_weight_file(tmp_path)
        cfg = BatchForecastConfig(
            model_id="m",
            batch_size=4,
            context_length=4,
            prediction_length=3,
            num_samples=1,
            device="cpu",
        )
        adapter = FoundationForecastAdapter(config=cfg, weight_path=path, weight_hash=whash)
        import torch

        # Only 2 rows of context, but batch_size=4 — should tile.
        ctx = torch.randn(2, 4)
        result = adapter.forecast(ctx)
        assert len(result.predictions) == 4

    def test_forecast_slices_large_batch(self, tmp_path: Path) -> None:
        path, whash = self._make_weight_file(tmp_path)
        cfg = BatchForecastConfig(
            model_id="m",
            batch_size=2,
            context_length=4,
            prediction_length=3,
            num_samples=1,
            device="cpu",
        )
        adapter = FoundationForecastAdapter(config=cfg, weight_path=path, weight_hash=whash)
        import torch

        # 8 rows of context, but batch_size=2 — should slice.
        ctx = torch.randn(8, 4)
        result = adapter.forecast(ctx)
        assert len(result.predictions) == 2

    def test_forecast_with_list_input(self, tmp_path: Path) -> None:
        path, whash = self._make_weight_file(tmp_path)
        cfg = BatchForecastConfig(
            model_id="m",
            batch_size=2,
            context_length=3,
            prediction_length=2,
            num_samples=1,
            device="cpu",
        )
        adapter = FoundationForecastAdapter(config=cfg, weight_path=path, weight_hash=whash)
        ctx = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
        result = adapter.forecast(ctx)
        assert len(result.predictions) == 2
        assert all(len(row) == 2 for row in result.predictions)

    def test_forecast_missing_weight_raises(self, tmp_path: Path) -> None:
        cfg = BatchForecastConfig(model_id="m", batch_size=2, context_length=4, device="cpu")
        adapter = FoundationForecastAdapter(
            config=cfg,
            weight_path=str(tmp_path / "nonexistent.pt"),
            weight_hash="x",
        )
        import torch

        ctx = torch.randn(2, 4)
        with pytest.raises(FileNotFoundError):
            adapter.forecast(ctx)

    def test_forecast_num_samples_noise(self, tmp_path: Path) -> None:
        path, whash = self._make_weight_file(tmp_path)
        cfg = BatchForecastConfig(
            model_id="m",
            batch_size=2,
            context_length=4,
            prediction_length=3,
            num_samples=100,
            seed=42,
            device="cpu",
        )
        adapter = FoundationForecastAdapter(config=cfg, weight_path=path, weight_hash=whash)
        import torch

        ctx = torch.randn(2, 4)
        result = adapter.forecast(ctx)
        assert len(result.predictions) == 2
        # With noise, predictions should still be deterministic (seeded).
        adapter2 = FoundationForecastAdapter(config=cfg, weight_path=path, weight_hash=whash)
        result2 = adapter2.forecast(ctx)
        assert result.predictions == result2.predictions


# ---------------------------------------------------------------------------
# FoundationHealthcheck
# ---------------------------------------------------------------------------


class TestFoundationHealthcheck:
    def test_init_default(self) -> None:
        hc = FoundationHealthcheck()
        assert hc.timeout_seconds == 60
        assert hc.weight_cache_dir == "/opt/foundation_weights"

    def test_init_custom(self, tmp_path: Path) -> None:
        hc = FoundationHealthcheck(timeout_seconds=30, weight_cache_dir=str(tmp_path))
        assert hc.timeout_seconds == 30
        assert hc.weight_cache_dir == str(tmp_path)

    def test_init_invalid_timeout(self) -> None:
        with pytest.raises(ValueError):
            FoundationHealthcheck(timeout_seconds=0)
        with pytest.raises(ValueError):
            FoundationHealthcheck(timeout_seconds=-1)

    def test_run_returns_dict(self, tmp_path: Path) -> None:
        hc = FoundationHealthcheck(weight_cache_dir=str(tmp_path))
        status = hc.run()
        assert isinstance(status, dict)
        assert "healthy" in status
        assert "gpu" in status
        assert "weight_cache" in status
        assert "forecast" in status
        assert "error" in status
        assert "duration_seconds" in status

    def test_run_weight_cache_accessible(self, tmp_path: Path) -> None:
        hc = FoundationHealthcheck(weight_cache_dir=str(tmp_path))
        status = hc.run()
        assert status["weight_cache"] is True

    def test_run_weight_cache_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist"
        hc = FoundationHealthcheck(weight_cache_dir=str(missing))
        status = hc.run()
        assert status["weight_cache"] is False
        assert status["healthy"] is False

    def test_run_forecast_succeeds_on_cpu(self, tmp_path: Path) -> None:
        # On a CPU-only host the forecast probe still runs (it uses a
        # self-contained projection), but healthy is False because the
        # GPU is unavailable.
        hc = FoundationHealthcheck(weight_cache_dir=str(tmp_path))
        status = hc.run()
        assert status["forecast"] is True

    def test_is_healthy_false_on_cpu_host(self, tmp_path: Path) -> None:
        hc = FoundationHealthcheck(weight_cache_dir=str(tmp_path))
        # CPU-only host -> GPU not available -> not healthy.
        assert hc.is_healthy() is False

    def test_run_gpu_status_present(self, tmp_path: Path) -> None:
        hc = FoundationHealthcheck(weight_cache_dir=str(tmp_path))
        status = hc.run()
        assert status["gpu"] is not None
        assert "available" in status["gpu"]

    def test_run_duration_positive(self, tmp_path: Path) -> None:
        hc = FoundationHealthcheck(weight_cache_dir=str(tmp_path))
        status = hc.run()
        assert status["duration_seconds"] >= 0.0

    def test_run_healthy_false_when_any_probe_fails(self, tmp_path: Path) -> None:
        # Weight cache missing -> healthy False even if forecast works.
        hc = FoundationHealthcheck(weight_cache_dir=str(tmp_path / "nope"))
        status = hc.run()
        assert status["healthy"] is False
