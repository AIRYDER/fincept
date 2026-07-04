"""
quant_foundry.sequence_runtime — PyTorch sequence runtime (T-10.5).

This module provides a self-contained, importable PyTorch runtime for
sequence models (RNN/Transformer-style) used by the quant foundry's GPU
worker path. It is designed to be **importable without torch installed** —
all torch imports are lazy and performed inside methods, so the module can
be imported on CPU-only machines (e.g. the local test suite) and only fails
when a torch-dependent operation is actually invoked.

Capabilities:

- :class:`SequenceImageSpec` — declarative spec for the
  ``trainer-gpu-sequence`` Docker image (base image, packages,
  healthcheck command).
- :class:`CheckpointConfig` — configuration for checkpoint save/load +
  resume (Pydantic v2, frozen + ``extra='forbid'``).
- :class:`MixedPrecisionConfig` — configuration for mixed precision
  training (float16 / bfloat16, grad scaler).
- :class:`MetricsArtifact` — typed per-epoch metrics artifact.
- :class:`SequenceTensorLoader` — loads sequence tensors from ``.npz``
  or sharded parquet, with shape validation and batch slicing.
- :class:`CheckpointManager` — saves / loads / cleans up training
  checkpoints with a rolling max.
- :class:`MixedPrecisionManager` — wraps torch autocast + GradScaler.
- :class:`SequenceHealthcheck` — healthcheck that probes the GPU, loads
  a tiny sequence tensor, and runs a tiny forward pass.

Design notes:

- **Lazy torch import.** ``import torch`` happens inside methods, never at
  module top level. The module can be imported, and the Pydantic models /
  ``SequenceImageSpec`` can be constructed, on a host without torch.
- **No live trading authority.** The healthcheck trains on synthetic data
  only; it never touches real feature-lake data or produces tradeable
  predictions.
- **No secrets.** Configs carry only hyperparameters and filesystem paths
  — never credentials.
- **Cost fails closed.** The healthcheck reports unhealthy when the GPU is
  unavailable or any probe raises; it never reports healthy on a partial
  probe.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Docker image spec
# ---------------------------------------------------------------------------


class SequenceImageSpec(BaseModel):
    """Declarative spec for the ``trainer-gpu-sequence`` Docker image.

    Frozen + ``extra='forbid'`` for audit integrity. The spec is the source
    of truth for the image's base, packages, and healthcheck command; the
    Dockerfile in ``docker/trainer-gpu-sequence/`` is generated from it
    (kept in sync by review).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    image_name: str = "trainer-gpu-sequence"
    base_image: str = "pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime"
    python_version: str = "3.12"
    packages: list[str] = Field(
        default_factory=lambda: [
            "torch==2.1.0",
            "numpy>=1.26",
            "pandas>=2.1",
            "scikit-learn>=1.3",
            "pydantic>=2.7",
            "einops>=0.7",
        ]
    )
    gpu_required: bool = True
    healthcheck_cmd: str = (
        'python -c "from quant_foundry.sequence_runtime import '
        "SequenceHealthcheck; import sys; "
        'sys.exit(0 if SequenceHealthcheck().is_healthy() else 1)"'
    )
    supports_mixed_precision: bool = True
    supports_checkpoint_resume: bool = True


# ---------------------------------------------------------------------------
# Checkpoint config
# ---------------------------------------------------------------------------


class CheckpointConfig(BaseModel):
    """Configuration for checkpoint save/load + resume.

    Frozen + ``extra='forbid'`` for audit integrity. ``checkpoint_dir`` is
    the directory where checkpoints are written; ``resume_from_checkpoint``
    is an optional explicit path to resume from (otherwise the latest
    checkpoint in ``checkpoint_dir`` is used).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint_dir: str
    save_every_n_epochs: int = 1
    max_checkpoints: int = 3
    resume_from_checkpoint: str | None = None

    @field_validator("save_every_n_epochs")
    @classmethod
    def _validate_save_every(cls, v: int) -> int:
        if v < 1:
            raise ValueError("save_every_n_epochs must be >= 1")
        return v

    @field_validator("max_checkpoints")
    @classmethod
    def _validate_max_checkpoints(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_checkpoints must be >= 1")
        return v


# ---------------------------------------------------------------------------
# Mixed precision config
# ---------------------------------------------------------------------------


class MixedPrecisionConfig(BaseModel):
    """Configuration for mixed precision training.

    Frozen + ``extra='forbid'`` for audit integrity. ``dtype`` must be one
    of ``"float16"`` or ``"bfloat16"``. ``grad_scaler`` controls whether a
    :class:`torch.cuda.amp.GradScaler` is used (only meaningful for
    ``float16`` on CUDA).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    dtype: str = "float16"
    grad_scaler: bool = True

    @field_validator("dtype")
    @classmethod
    def _validate_dtype(cls, v: str) -> str:
        if v not in ("float16", "bfloat16"):
            raise ValueError("dtype must be one of 'float16', 'bfloat16'")
        return v


# ---------------------------------------------------------------------------
# Metrics artifact
# ---------------------------------------------------------------------------


class MetricsArtifact(BaseModel):
    """Typed per-epoch metrics artifact.

    Frozen + ``extra='forbid'`` for audit integrity. ``val_loss`` and
    ``gpu_memory_mb`` are optional (e.g. when no validation set is used or
    the GPU is unavailable).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    epoch: int
    train_loss: float
    val_loss: float | None = None
    learning_rate: float
    gpu_memory_mb: float | None = None
    epoch_duration_seconds: float
    timestamp: str


# ---------------------------------------------------------------------------
# Sequence tensor loader
# ---------------------------------------------------------------------------


class SequenceTensorLoader:
    """Loads sequence tensors from ``.npz`` or sharded parquet.

    Supports two on-disk formats:

    - ``.npz`` — a NumPy archive with arrays keyed by name (e.g.
      ``sequences``, ``targets``). Loaded via ``numpy.load``.
    - Sharded parquet — a directory of ``*.parquet`` files, each row
      carrying a ``sequence`` (list of floats) and ``target`` column.
      Loaded via ``pandas.read_parquet`` (concatenated across shards).

    All numpy / torch / pandas imports are lazy (inside methods), so the
    loader can be constructed on a host without those packages.
    """

    def __init__(
        self,
        data_path: str,
        manifest_data: dict | None = None,
    ) -> None:
        self.data_path = data_path
        self.manifest_data = manifest_data
        self._data: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        """Load sequence tensors from disk and return them as a dict.

        For ``.npz`` files the dict maps array names to numpy arrays. For
        sharded parquet the dict has ``sequences`` (numpy 2D array) and
        ``targets`` (numpy 1D array) keys. The loaded data is cached on
        ``self._data`` so repeated calls return the same dict.

        Raises:
            FileNotFoundError: if ``data_path`` does not exist.
            ValueError: if the format is unsupported.
        """
        if self._data is not None:
            return self._data

        path = Path(self.data_path)
        if not path.exists():
            raise FileNotFoundError(f"data path not found: {self.data_path}")

        if path.is_file() and path.suffix == ".npz":
            import numpy as np

            with np.load(str(path), allow_pickle=False) as npz:
                self._data = {k: npz[k] for k in npz.files}
            return self._data

        if path.is_dir():
            # Sharded parquet directory.
            import numpy as np
            import pandas as pd

            shards = sorted(path.glob("*.parquet"))
            if not shards:
                raise ValueError(f"no parquet shards found in directory: {self.data_path}")
            frames = [pd.read_parquet(str(s)) for s in shards]
            df = pd.concat(frames, ignore_index=True)

            sequences = np.array(
                [np.asarray(seq, dtype=np.float32) for seq in df["sequence"]],
                dtype=np.float32,
            )
            targets = (
                np.asarray(df["target"].to_numpy(), dtype=np.float32)
                if "target" in df.columns
                else None
            )
            self._data = {"sequences": sequences, "targets": targets}
            return self._data

        raise ValueError(
            f"unsupported data format: {self.data_path} "
            "(expected .npz file or directory of .parquet shards)"
        )

    def validate_shape(self, expected_shape: tuple[int, ...]) -> bool:
        """Validate that the loaded primary tensor matches ``expected_shape``.

        The "primary" tensor is ``sequences`` for parquet loads, or the
        first array for ``.npz`` loads. Returns ``True`` if the shape
        matches, ``False`` otherwise. Raises if data has not been loaded
        and cannot be loaded.
        """
        data = self.load()
        if "sequences" in data:
            arr = data["sequences"]
        else:
            # First array in the npz dict.
            arr = next(iter(data.values()))
        return tuple(arr.shape) == tuple(expected_shape)

    def get_batch(self, indices: list[int]) -> dict[str, Any]:
        """Return a batch of sequences at the given ``indices``.

        Returns a dict with ``sequences`` (and ``targets`` if present)
        sliced to the requested indices. Raises ``ValueError`` if
        ``indices`` is empty.
        """
        if not indices:
            raise ValueError("indices must be non-empty")

        data = self.load()
        import numpy as np

        batch: dict[str, Any] = {}
        if "sequences" in data:
            batch["sequences"] = data["sequences"][indices]
            if data.get("targets") is not None:
                batch["targets"] = data["targets"][indices]
            return batch

        # npz: slice every array by the first axis.
        for key, arr in data.items():
            batch[key] = np.asarray(arr)[indices]
        return batch


# ---------------------------------------------------------------------------
# Checkpoint manager
# ---------------------------------------------------------------------------


class CheckpointManager:
    """Saves, loads, and cleans up training checkpoints.

    Checkpoints are written to ``config.checkpoint_dir`` as
    ``checkpoint_epoch_{n}.pt`` files. Each checkpoint is a dict with
    ``model_state``, ``optimizer_state``, ``epoch``, ``metrics``, and a
    ``timestamp``. At most ``config.max_checkpoints`` files are kept;
    older ones are removed by :meth:`cleanup`.

    All torch imports are lazy (inside methods).
    """

    def __init__(self, config: CheckpointConfig) -> None:
        self.config = config
        self._dir = Path(config.checkpoint_dir)

    def _checkpoint_path(self, epoch: int) -> Path:
        """Return the path for a given epoch's checkpoint file."""
        return self._dir / f"checkpoint_epoch_{epoch}.pt"

    def save(
        self,
        model_state: dict,
        optimizer_state: dict,
        epoch: int,
        metrics: dict,
    ) -> str:
        """Save a checkpoint and return its path.

        Creates ``checkpoint_dir`` if it does not exist. The checkpoint
        dict is written via ``torch.save``.

        Args:
            model_state: the model's ``state_dict``.
            optimizer_state: the optimizer's ``state_dict``.
            epoch: the epoch number (0-indexed).
            metrics: a dict of metric name -> value.

        Returns:
            The string path to the written checkpoint file.
        """
        import torch

        self._dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "model_state": model_state,
            "optimizer_state": optimizer_state,
            "epoch": epoch,
            "metrics": metrics,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        path = self._checkpoint_path(epoch)
        torch.save(payload, str(path))
        return str(path)

    def load(self, path: str) -> dict:
        """Load a checkpoint from ``path`` and return its dict.

        Raises:
            FileNotFoundError: if ``path`` does not exist.
            ValueError: if the loaded payload is not a dict.
        """
        import torch

        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"checkpoint not found: {path}")
        payload = torch.load(str(p), map_location="cpu")
        if not isinstance(payload, dict):
            raise ValueError(f"checkpoint payload is not a dict: {type(payload).__name__}")
        return payload

    def latest_checkpoint(self) -> str | None:
        """Return the path to the latest checkpoint, or ``None`` if none.

        "Latest" is determined by the epoch number encoded in the
        filename (``checkpoint_epoch_{n}.pt``), not by mtime.
        """
        if not self._dir.exists():
            return None
        checkpoints: list[tuple[int, Path]] = []
        for f in self._dir.glob("checkpoint_epoch_*.pt"):
            stem = f.stem.replace("checkpoint_epoch_", "")
            try:
                epoch = int(stem)
            except ValueError:
                continue
            checkpoints.append((epoch, f))
        if not checkpoints:
            return None
        checkpoints.sort(key=lambda item: item[0])
        return str(checkpoints[-1][1])

    def cleanup(self) -> None:
        """Remove old checkpoints beyond ``config.max_checkpoints``.

        Keeps the most recent ``max_checkpoints`` checkpoints (by epoch
        number) and deletes the rest.
        """
        if not self._dir.exists():
            return
        checkpoints: list[tuple[int, Path]] = []
        for f in self._dir.glob("checkpoint_epoch_*.pt"):
            stem = f.stem.replace("checkpoint_epoch_", "")
            try:
                epoch = int(stem)
            except ValueError:
                continue
            checkpoints.append((epoch, f))
        if len(checkpoints) <= self.config.max_checkpoints:
            return
        checkpoints.sort(key=lambda item: item[0])
        excess = checkpoints[: len(checkpoints) - self.config.max_checkpoints]
        for _epoch, path in excess:
            try:
                path.unlink()
            except OSError:
                continue


# ---------------------------------------------------------------------------
# Mixed precision manager
# ---------------------------------------------------------------------------


class MixedPrecisionManager:
    """Wraps torch autocast + GradScaler for mixed precision training.

    When ``config.enabled`` is ``False``, :meth:`autocast_context` returns
    a nullcontext and the scaler methods are no-ops, so the manager can be
    used unconditionally without affecting CPU / disabled runs.

    All torch imports are lazy (inside methods).
    """

    def __init__(self, config: MixedPrecisionConfig) -> None:
        self.config = config
        self._scaler: Any = None

    def _torch_dtype(self) -> Any:
        """Map the config dtype string to a torch dtype."""
        import torch

        if self.config.dtype == "float16":
            return torch.float16
        if self.config.dtype == "bfloat16":
            return torch.bfloat16
        raise ValueError(f"unsupported dtype: {self.config.dtype}")

    def autocast_context(self) -> Any:
        """Return an autocast context manager.

        When disabled, returns ``contextlib.nullcontext`` so the caller can
        use ``with mgr.autocast_context():`` unconditionally. When enabled
        on CPU with bfloat16, uses ``torch.autocast('cpu', ...)``; on CUDA
        uses ``torch.autocast('cuda', ...)``.
        """
        import contextlib

        import torch

        if not self.config.enabled:
            return contextlib.nullcontext()

        dtype = self._torch_dtype()
        device_type = "cuda" if torch.cuda.is_available() else "cpu"
        # bfloat16 autocast is supported on CPU; float16 autocast on CPU
        # is a no-op but torch allows it.
        return torch.autocast(device_type=device_type, dtype=dtype)

    def _get_scaler(self) -> Any:
        """Lazily build and cache the GradScaler (or None)."""
        if self._scaler is not None:
            return self._scaler
        if not self.config.enabled or not self.config.grad_scaler:
            self._scaler = False  # sentinel: no scaler
            return self._scaler
        import torch

        # GradScaler is only meaningful for float16 on CUDA. On CPU or
        # bfloat16 we skip it.
        if self.config.dtype == "float16" and torch.cuda.is_available():
            self._scaler = torch.cuda.amp.GradScaler()
        else:
            self._scaler = False
        return self._scaler

    def scale_loss(self, loss: Any) -> Any:
        """Scale ``loss`` for mixed precision (returns ``scaled_loss``).

        When the scaler is disabled, returns ``loss`` unchanged.
        """
        scaler = self._get_scaler()
        if scaler is False:
            return loss
        return scaler.scale(loss)

    def step_optimizer(self, optimizer: Any) -> None:
        """Step ``optimizer`` with the scaler (or directly when disabled)."""
        scaler = self._get_scaler()
        if scaler is False:
            optimizer.step()
            return
        scaler.step(optimizer)

    def update(self) -> None:
        """Update the scaler (no-op when disabled)."""
        scaler = self._get_scaler()
        if scaler is False:
            return
        scaler.update()


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------


class SequenceHealthcheck:
    """Healthcheck for the sequence runtime.

    Probes the GPU via :func:`check_gpu` (reused from
    ``tabular_neural_runtime``), loads a tiny synthetic sequence tensor,
    and runs a tiny forward pass through a 1-layer LSTM. Used by the GPU
    worker's ``HEALTHCHECK`` step to fail fast when the runtime is broken
    or the GPU is missing.
    """

    def __init__(self, timeout_seconds: int = 60) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds

    def run(self) -> dict[str, Any]:
        """Run the healthcheck and return a status dict.

        The dict contains:

        - ``healthy`` (bool): overall health.
        - ``gpu`` (dict): serialized :class:`GPUStatus`.
        - ``tensor_load`` (bool): whether the synthetic tensor load succeeded.
        - ``forward_pass`` (bool): whether the tiny forward pass succeeded.
        - ``error`` (str | None): error message if the check failed.
        - ``duration_seconds`` (float): wall-clock duration.
        """
        from quant_foundry.tabular_neural_runtime import check_gpu

        start = time.perf_counter()
        result: dict[str, Any] = {
            "healthy": False,
            "gpu": None,
            "tensor_load": False,
            "forward_pass": False,
            "error": None,
            "duration_seconds": 0.0,
        }

        try:
            gpu_status = check_gpu()
            result["gpu"] = gpu_status.model_dump()
        except Exception as exc:  # pragma: no cover - defensive
            result["error"] = f"gpu probe failed: {exc}"
            result["duration_seconds"] = time.perf_counter() - start
            return result

        # Tiny synthetic sequence tensor load + forward pass on CPU.
        try:
            import torch
            import torch.nn as nn

            # Synthetic sequence: (batch=4, seq_len=8, features=3).
            x = torch.randn(4, 8, 3)
            result["tensor_load"] = True

            model = nn.LSTM(input_size=3, hidden_size=8, batch_first=True)
            out, _ = model(x)
            # Force a reduction so the output is a scalar-ish value.
            _ = float(out.sum().item())
            result["forward_pass"] = True
        except Exception as exc:
            result["error"] = f"sequence probe failed: {exc}"

        gpu_ok = bool(result["gpu"].get("available")) if result["gpu"] else False
        result["healthy"] = bool(
            gpu_ok and result["tensor_load"] and result["forward_pass"] and result["error"] is None
        )
        result["duration_seconds"] = time.perf_counter() - start
        return result

    def is_healthy(self) -> bool:
        """Return ``True`` if the GPU is available and both probes succeed.

        Note: on a CPU-only host this returns ``False`` because the GPU is
        not available. The healthcheck is intended for the GPU worker
        container, where a missing GPU is a hard failure.
        """
        status = self.run()
        return bool(status.get("healthy"))


__all__ = [
    "CheckpointConfig",
    "CheckpointManager",
    "MetricsArtifact",
    "MixedPrecisionConfig",
    "MixedPrecisionManager",
    "SequenceHealthcheck",
    "SequenceImageSpec",
    "SequenceTensorLoader",
]
