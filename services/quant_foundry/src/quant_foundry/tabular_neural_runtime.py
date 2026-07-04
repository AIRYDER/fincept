"""
quant_foundry.tabular_neural_runtime — PyTorch tabular neural runtime (T-9.1).

This module provides a self-contained, importable PyTorch runtime for tabular
neural networks used by the quant foundry's GPU worker path. It is designed to
be **importable without torch installed** — all torch imports are lazy and
performed inside methods, so the module can be imported on CPU-only machines
(e.g. the local test suite) and only fails when a torch-dependent operation is
actually invoked.

Capabilities:

- :class:`GPUStatus` / :class:`GPUMemorySnapshot` — typed telemetry for the
  CUDA device and its memory state (Pydantic v2, frozen + ``extra='forbid'``).
- :func:`check_gpu` / :func:`get_gpu_memory_snapshot` — probe helpers that
  degrade gracefully to "no GPU" on CPU-only hosts.
- :class:`TinyTabularNet` — a small configurable MLP (BatchNorm + Dropout +
  ReLU, sigmoid or raw output) used for canary training and smoke tests.
- :class:`NeuralCanaryConfig` / :class:`NeuralCanaryResult` — config + result
  for the canary training run.
- :func:`run_neural_canary` — runs a tiny synthetic training loop, records
  GPU memory snapshots, and optionally saves a state_dict artifact.
- :func:`save_neural_artifact` / :func:`load_neural_artifact` — round-trip
  state_dict persistence.
- :class:`TabularNeuralHealthcheck` — healthcheck that probes the GPU and
  runs a 1-epoch canary; used by the GPU worker's ``HEALTHCHECK`` step.
- :class:`ImageSpec` — declarative spec for the
  ``trainer-gpu-tabular-neural`` Docker image (base image, packages,
  healthcheck command).

Design notes:

- **Lazy torch import.** ``import torch`` happens inside methods, never at
  module top level. The module can be imported, and the Pydantic models /
  ``ImageSpec`` can be constructed, on a host without torch.
- **No live trading authority.** The canary trains on synthetic data only;
  it never touches real feature-lake data or produces tradeable predictions.
- **No secrets.** Configs carry only architecture + optimizer hyperparameters
  and a device string — never credentials or filesystem paths beyond the
  optional artifact path.
- **Cost fails closed.** The healthcheck reports unhealthy when the GPU is
  unavailable or the canary raises; it never reports healthy on a partial
  probe.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Telemetry models
# ---------------------------------------------------------------------------


class GPUStatus(BaseModel):
    """Typed snapshot of CUDA device availability and memory state.

    Frozen + ``extra='forbid'`` for audit integrity. On a CPU-only host
    every field except ``available`` is ``None``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    available: bool
    device_name: str | None = None
    cuda_version: str | None = None
    memory_total_mb: float | None = None
    memory_free_mb: float | None = None
    memory_used_mb: float | None = None


class GPUMemorySnapshot(BaseModel):
    """A point-in-time snapshot of GPU memory usage.

    ``allocated_mb`` / ``reserved_mb`` come from PyTorch's caching
    allocator (``torch.cuda.memory_allocated`` / ``memory_reserved``).
    ``free_mb`` is the driver-reported free memory, when available.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allocated_mb: float
    reserved_mb: float
    free_mb: float | None = None
    timestamp: str


# ---------------------------------------------------------------------------
# Canary config + result
# ---------------------------------------------------------------------------


class NeuralCanaryConfig(BaseModel):
    """Configuration for a tabular neural canary training run.

    The canary trains a :class:`TinyTabularNet` on synthetic data — it is a
    smoke test for the runtime, not a production model. Defaults are tiny so
    the canary completes in well under a second on CPU.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_dim: int = 10
    hidden_dims: list[int] = Field(default_factory=lambda: [32, 16])
    output_dim: int = 1
    learning_rate: float = 0.001
    epochs: int = 5
    batch_size: int = 32
    device: str = "auto"  # one of: auto, cpu, cuda
    seed: int = 42


class NeuralCanaryResult(BaseModel):
    """Result of a tabular neural canary training run.

    Frozen + ``extra='forbid'`` for audit integrity. ``memory_snapshots``
    is one snapshot per epoch (plus an initial snapshot before training).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config: NeuralCanaryConfig
    final_loss: float
    gpu_status: GPUStatus
    memory_snapshots: list[GPUMemorySnapshot]
    artifact_path: str | None = None
    trained: bool
    duration_seconds: float


# ---------------------------------------------------------------------------
# GPU probe helpers
# ---------------------------------------------------------------------------


def check_gpu() -> GPUStatus:
    """Probe CUDA availability and return a :class:`GPUStatus`.

    Performs a lazy ``import torch`` and reads
    ``torch.cuda.is_available()``. On a CPU-only host (or when torch is
    not installed) returns ``GPUStatus(available=False, ...)`` with all
    other fields ``None``.
    """
    try:
        import torch
    except Exception:
        # torch not installed — no GPU available from this runtime's
        # perspective.
        return GPUStatus(available=False)

    if not torch.cuda.is_available():
        return GPUStatus(available=False)

    device_idx = 0
    device_name = torch.cuda.get_device_name(device_idx)
    cuda_version = torch.version.cuda

    props = torch.cuda.get_device_properties(device_idx)
    memory_total_mb = float(props.total_memory) / (1024.0 * 1024.0)

    free_bytes, total_bytes = torch.cuda.mem_get_info(device_idx)
    memory_free_mb = float(free_bytes) / (1024.0 * 1024.0)
    memory_used_mb = float(total_bytes - free_bytes) / (1024.0 * 1024.0)

    return GPUStatus(
        available=True,
        device_name=device_name,
        cuda_version=str(cuda_version) if cuda_version is not None else None,
        memory_total_mb=memory_total_mb,
        memory_free_mb=memory_free_mb,
        memory_used_mb=memory_used_mb,
    )


def get_gpu_memory_snapshot() -> GPUMemorySnapshot:
    """Capture the current GPU memory state as a :class:`GPUMemorySnapshot`.

    Returns a zero-allocated / zero-reserved snapshot (with ``free_mb=None``)
    on a CPU-only host. The timestamp is an ISO-8601 UTC string.
    """
    timestamp = datetime.now(UTC).isoformat()

    try:
        import torch
    except Exception:
        return GPUMemorySnapshot(
            allocated_mb=0.0,
            reserved_mb=0.0,
            free_mb=None,
            timestamp=timestamp,
        )

    if not torch.cuda.is_available():
        return GPUMemorySnapshot(
            allocated_mb=0.0,
            reserved_mb=0.0,
            free_mb=None,
            timestamp=timestamp,
        )

    device_idx = 0
    allocated_mb = float(torch.cuda.memory_allocated(device_idx)) / (1024.0 * 1024.0)
    reserved_mb = float(torch.cuda.memory_reserved(device_idx)) / (1024.0 * 1024.0)
    free_bytes, _total_bytes = torch.cuda.mem_get_info(device_idx)
    free_mb = float(free_bytes) / (1024.0 * 1024.0)

    return GPUMemorySnapshot(
        allocated_mb=allocated_mb,
        reserved_mb=reserved_mb,
        free_mb=free_mb,
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# Tiny tabular MLP
# ---------------------------------------------------------------------------


class TinyTabularNet:
    """A small configurable MLP for tabular canary training.

    Architecture: ``input_dim`` -> ``hidden_dims`` (each followed by
    BatchNorm1d + Dropout + ReLU) -> ``output_dim``. The final layer is
    raw (regression) by default; pass ``binary=True`` to apply a Sigmoid
    to the output for binary classification.

    This is a thin wrapper around ``torch.nn.Module``. It is defined as a
    regular class (not a subclass of ``nn.Module`` at the type level) so
    that the module remains importable without torch — the actual
    ``nn.Module`` subclass is built lazily inside :meth:`_build_module`.
    """

    def __init__(
        self,
        input_dim: int,
        hidden_dims: list[int],
        output_dim: int = 1,
        dropout: float = 0.1,
        binary: bool = False,
    ) -> None:
        if input_dim <= 0:
            raise ValueError("input_dim must be positive")
        if output_dim <= 0:
            raise ValueError("output_dim must be positive")
        if not hidden_dims:
            raise ValueError("hidden_dims must be non-empty")
        if any(h <= 0 for h in hidden_dims):
            raise ValueError("all hidden_dims must be positive")
        if not 0.0 <= dropout < 1.0:
            raise ValueError("dropout must be in [0, 1)")
        self.input_dim = input_dim
        self.hidden_dims = list(hidden_dims)
        self.output_dim = output_dim
        self.dropout = dropout
        self.binary = binary
        self._module: Any = None

    def _build_module(self) -> Any:
        """Build and return the underlying ``torch.nn.Module``.

        Lazily imports torch. The built module is cached on
        ``self._module`` so repeated calls return the same instance.
        """
        if self._module is not None:
            return self._module

        import torch.nn as nn

        layers: list[Any] = []
        prev = self.input_dim
        for h in self.hidden_dims:
            layers.append(nn.Linear(prev, h))
            layers.append(nn.BatchNorm1d(h))
            layers.append(nn.Dropout(self.dropout))
            layers.append(nn.ReLU())
            prev = h
        layers.append(nn.Linear(prev, self.output_dim))
        if self.binary:
            layers.append(nn.Sigmoid())

        net = nn.Sequential(*layers)
        self._module = net
        return net

    @property
    def module(self) -> Any:
        """Return the underlying ``torch.nn.Module``, building it if needed."""
        return self._build_module()

    def forward(self, x: Any) -> Any:
        """Run a forward pass of the underlying module."""
        return self.module(x)

    def parameters(self) -> Any:
        """Return the underlying module's parameters iterator."""
        return self.module.parameters()

    def state_dict(self) -> dict[str, Any]:
        """Return the underlying module's state_dict."""
        return self.module.state_dict()

    def load_state_dict(self, state_dict: dict[str, Any]) -> None:
        """Load a state_dict into the underlying module."""
        self.module.load_state_dict(state_dict)

    def to(self, device: Any) -> TinyTabularNet:
        """Move the underlying module to ``device`` and return self."""
        self._module = self.module.to(device)
        return self

    def train(self, mode: bool = True) -> TinyTabularNet:
        """Set the underlying module's train/eval mode and return self."""
        self.module.train(mode)
        return self

    def eval(self) -> TinyTabularNet:
        """Set the underlying module to eval mode and return self."""
        return self.train(False)


# ---------------------------------------------------------------------------
# Canary training
# ---------------------------------------------------------------------------


def _resolve_device(config: NeuralCanaryConfig, gpu_status: GPUStatus) -> Any:
    """Resolve the torch device for a canary run.

    ``auto`` picks CUDA when available, else CPU. ``cpu`` / ``cuda`` are
    honored literally (cuda on a CPU-only host falls back to CPU).
    """
    import torch

    if config.device == "cpu":
        return torch.device("cpu")
    if config.device == "cuda":
        if gpu_status.available:
            return torch.device("cuda")
        return torch.device("cpu")
    # auto
    return torch.device("cuda" if gpu_status.available else "cpu")


def run_neural_canary(
    config: NeuralCanaryConfig,
    artifact_path: str | None = None,
) -> NeuralCanaryResult:
    """Run a tabular neural canary training loop.

    Generates synthetic data (``input_dim`` features, random labels),
    builds a :class:`TinyTabularNet`, trains for ``config.epochs`` epochs
    with MSE loss and Adam, records a :class:`GPUMemorySnapshot` per
    epoch (plus an initial snapshot before training), and optionally
    saves the trained state_dict to ``artifact_path``.

    Args:
        config: Canary training configuration.
        artifact_path: Optional path to save the trained state_dict. If
            ``None`` no artifact is written.

    Returns:
        A :class:`NeuralCanaryResult` with the final loss, GPU status,
        per-epoch memory snapshots, and artifact path (if any).
    """
    import torch
    import torch.nn as nn

    start = time.perf_counter()

    # Validate config.
    if config.epochs < 0:
        raise ValueError("epochs must be >= 0")
    if config.batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if config.learning_rate <= 0:
        raise ValueError("learning_rate must be positive")

    torch.manual_seed(config.seed)

    gpu_status = check_gpu()
    device = _resolve_device(config, gpu_status)

    # Synthetic data: 10 * batch_size samples.
    n_samples = max(config.batch_size * 10, config.batch_size)
    x = torch.randn(n_samples, config.input_dim)
    y = torch.randn(n_samples, config.output_dim)

    model = TinyTabularNet(
        input_dim=config.input_dim,
        hidden_dims=list(config.hidden_dims),
        output_dim=config.output_dim,
        binary=False,
    )
    model.to(device)
    model.train()

    loss_fn = nn.MSELoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=config.learning_rate)

    snapshots: list[GPUMemorySnapshot] = [get_gpu_memory_snapshot()]

    final_loss = float("nan")
    trained = False
    if config.epochs > 0:
        for _epoch in range(config.epochs):
            # Mini-batch over the synthetic data.
            permutation = torch.randperm(n_samples)
            epoch_losses: list[float] = []
            for i in range(0, n_samples, config.batch_size):
                idx = permutation[i : i + config.batch_size]
                batch_x = x[idx].to(device)
                batch_y = y[idx].to(device)

                # BatchNorm1d requires more than 1 sample per batch.
                if batch_x.shape[0] < 2:
                    continue

                optimizer.zero_grad()
                pred = model.forward(batch_x)
                loss = loss_fn(pred, batch_y)
                loss.backward()
                optimizer.step()
                epoch_losses.append(float(loss.item()))

            snapshots.append(get_gpu_memory_snapshot())

        final_loss = float(sum(epoch_losses) / len(epoch_losses)) if epoch_losses else float("nan")
        trained = True
    else:
        # Zero epochs: still produce a final snapshot.
        snapshots.append(get_gpu_memory_snapshot())
        final_loss = float("nan")
        trained = False

    saved_path: str | None = None
    if artifact_path is not None:
        save_neural_artifact(model, artifact_path)
        saved_path = artifact_path

    duration = time.perf_counter() - start

    return NeuralCanaryResult(
        config=config,
        final_loss=final_loss,
        gpu_status=gpu_status,
        memory_snapshots=snapshots,
        artifact_path=saved_path,
        trained=trained,
        duration_seconds=duration,
    )


# ---------------------------------------------------------------------------
# Artifact persistence
# ---------------------------------------------------------------------------


def save_neural_artifact(model: TinyTabularNet, path: str) -> None:
    """Save a :class:`TinyTabularNet` state_dict to ``path``.

    Creates parent directories as needed. The file is written via
    ``torch.save`` (state_dict only — no pickled module).
    """
    import torch

    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), str(p))


def load_neural_artifact(path: str, config: NeuralCanaryConfig) -> TinyTabularNet:
    """Load a state_dict from ``path`` into a new :class:`TinyTabularNet`.

    The new model is built from ``config`` (input_dim, hidden_dims,
    output_dim) and the saved state_dict is loaded into it. The model is
    returned in eval mode on CPU.
    """
    import torch

    model = TinyTabularNet(
        input_dim=config.input_dim,
        hidden_dims=list(config.hidden_dims),
        output_dim=config.output_dim,
        binary=False,
    )
    # Build the underlying module before loading state_dict.
    _ = model.module
    state_dict = torch.load(str(path), map_location="cpu")
    model.load_state_dict(state_dict)
    model.eval()
    return model


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------


class TabularNeuralHealthcheck:
    """Healthcheck for the tabular neural runtime.

    Probes the GPU via :func:`check_gpu` and runs a 1-epoch canary via
    :func:`run_neural_canary`. Used by the GPU worker's ``HEALTHCHECK``
    step to fail fast when the runtime is broken or the GPU is missing.
    """

    def __init__(self, timeout_seconds: int = 30) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds

    def run(self) -> dict[str, Any]:
        """Run the healthcheck and return a status dict.

        The dict contains:

        - ``healthy`` (bool): overall health.
        - ``gpu`` (dict): serialized :class:`GPUStatus`.
        - ``canary`` (dict | None): serialized :class:`NeuralCanaryResult`
          if the canary ran, else ``None`` with an ``error``.
        - ``error`` (str | None): error message if the check failed.
        - ``duration_seconds`` (float): wall-clock duration.
        """
        start = time.perf_counter()
        result: dict[str, Any] = {
            "healthy": False,
            "gpu": None,
            "canary": None,
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

        try:
            canary_config = NeuralCanaryConfig(
                input_dim=8,
                hidden_dims=[16, 8],
                output_dim=1,
                epochs=1,
                batch_size=16,
                device="auto",
            )
            canary_result = run_neural_canary(canary_config)
            result["canary"] = canary_result.model_dump()
            result["healthy"] = bool(canary_result.trained)
        except Exception as exc:
            result["error"] = f"canary failed: {exc}"

        result["duration_seconds"] = time.perf_counter() - start
        return result

    def is_healthy(self) -> bool:
        """Return ``True`` if the GPU is available and the canary trained.

        Note: on a CPU-only host this returns ``False`` because the GPU is
        not available. The healthcheck is intended for the GPU worker
        container, where a missing GPU is a hard failure.
        """
        status = self.run()
        gpu = status.get("gpu") or {}
        canary = status.get("canary") or {}
        gpu_ok = bool(gpu.get("available"))
        canary_ok = bool(canary.get("trained"))
        return bool(gpu_ok and canary_ok and status.get("error") is None)


# ---------------------------------------------------------------------------
# Docker image spec
# ---------------------------------------------------------------------------


class ImageSpec(BaseModel):
    """Declarative spec for the ``trainer-gpu-tabular-neural`` Docker image.

    Frozen + ``extra='forbid'`` for audit integrity. The spec is the source
    of truth for the image's base, packages, and healthcheck command; the
    Dockerfile in ``docker/trainer-gpu-tabular-neural/`` is generated from
    it (kept in sync by review).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    image_name: str = "trainer-gpu-tabular-neural"
    base_image: str = "pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime"
    python_version: str = "3.12"
    packages: list[str] = Field(
        default_factory=lambda: [
            "torch==2.1.0",
            "numpy>=1.26",
            "pandas>=2.1",
            "scikit-learn>=1.3",
            "pydantic>=2.7",
        ]
    )
    gpu_required: bool = True
    healthcheck_cmd: str = (
        'python -c "from quant_foundry.tabular_neural_runtime import '
        "TabularNeuralHealthcheck; import sys; "
        'sys.exit(0 if TabularNeuralHealthcheck().is_healthy() else 1)"'
    )


__all__ = [
    "GPUMemorySnapshot",
    "GPUStatus",
    "ImageSpec",
    "NeuralCanaryConfig",
    "NeuralCanaryResult",
    "TabularNeuralHealthcheck",
    "TinyTabularNet",
    "check_gpu",
    "get_gpu_memory_snapshot",
    "load_neural_artifact",
    "run_neural_canary",
    "save_neural_artifact",
]
