"""quant_foundry.patchtst_trainer — PatchTST canary trainer (T-10.3).

PatchTST (Patch Time Series Transformer) is a transformer-based model for
sequence modeling that patches contiguous segments of a time series into
tokens, projects them to a hidden dimension, adds positional encoding, and
runs them through a standard transformer encoder. Patching drastically
reduces the sequence length the transformer attends over (vs. per-timestep
tokenization), which both speeds up training and improves generalization on
long horizons.

This module provides a self-contained, importable PatchTST **canary**
trainer for the quant foundry's sequence-modeling GPU worker path. It builds
on the :mod:`quant_foundry.tabular_neural_runtime` runtime (for
:class:`GPUStatus` / :func:`check_gpu`) and integrates with the OOF artifact
writer (:mod:`quant_foundry.oof_artifacts`) and the model family registry
subsystem.

Capabilities:

- :class:`PatchTSTConfig` — frozen, ``extra='forbid'`` config for a PatchTST
  canary training run (architecture, optimizer, shadow-mode defaults).
- :class:`PatchTSTTrainingResult` — frozen, ``extra='forbid'`` result
  carrying epoch losses, GPU status, artifact paths, and promotion
  eligibility.
- :class:`PatchEmbedding` — an ``nn.Module`` that patches the input
  sequence, linearly projects patches to ``d_model``, and adds positional
  encoding.
- :class:`PatchTSTModel` — a PatchTST ``nn.Module`` with a patch embedding,
  transformer encoder (``n_layers`` layers, ``n_heads`` heads), and a
  linear output head.
- :class:`PatchTSTTrainer` — the train / predict / save / load / OOF-write
  façade used by the research dispatch path.
- :func:`validate_promotion_eligibility` — fail-closed promotion gate:
  shadow runs are only eligible with an explicit manual override.
- :func:`register_patchtst_family` — returns a
  :class:`~quant_foundry.alpha_genome.ModelFamilySpec`-compatible dict for
  PatchTST registration (does not mutate the registry itself).

Design notes (cross-cutting quant rigor, BIG_PLAN):

- **Shadow mode by default.** ``PatchTSTConfig.shadow_only`` defaults to
  ``True`` and ``PatchTSTTrainingResult.promotion_eligible`` is forced to
  ``False`` when shadow mode is on. Promotion requires an explicit manual
  override — there is no automatic path from shadow to production.
- **No live trading authority.** A shadow PatchTST run never produces
  tradeable predictions; its outputs are OOF predictions for ensemble
  integration and a model artifact for offline evaluation only.
- **No secrets.** Configs carry only architecture + optimizer
  hyperparameters, a device string, and a seed — never credentials or
  filesystem paths beyond the optional artifact path.
- **Cost fails closed.** Invalid configs are rejected at construction;
  training errors surface as exceptions rather than partial results.
- **Lazy torch import.** ``import torch`` happens inside methods, never at
  module top level, so this module is importable on hosts without torch
  (the Pydantic models and ``register_patchtst_family`` can be constructed
  without torch installed).
- **File-disjoint.** New module; does not modify ``real_trainer.py``,
  ``alpha_genome.py``, ``sequence_runtime.py``, or
  ``windowed_tensor_builder.py``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant_foundry.oof_artifacts import OOFWriter
from quant_foundry.tabular_neural_runtime import GPUStatus, check_gpu

# ---------------------------------------------------------------------------
# Config + result models
# ---------------------------------------------------------------------------


class PatchTSTConfig(BaseModel):
    """Configuration for a PatchTST canary training run.

    Frozen + ``extra='forbid'`` for audit integrity. Defaults are
    shadow-oriented: ``shadow_only=True``, a modest transformer
    (``d_model=64``, ``n_heads=4``, ``n_layers=2``), and a small patch
    length so a run completes quickly on CPU for smoke tests while still
    exercising the full PatchTST code path.

    Attributes:
        input_dim: Number of input channels (must be >= 1).
        seq_len: Window length — number of timesteps per sample (must be
            >= 1).
        patch_len: Patch length — number of timesteps per patch (must be
            >= 1 and <= ``seq_len``).
        stride: Patch stride — number of timesteps between consecutive
            patch starts (must be >= 1).
        d_model: Transformer hidden dimension (must be >= 1 and divisible
            by ``n_heads``).
        n_heads: Number of attention heads (must be >= 1).
        n_layers: Number of transformer encoder layers (must be >= 1).
        ff_dim: Feedforward network hidden dimension (must be >= 1).
        dropout: Dropout probability (must be in [0, 1)).
        output_dim: Dimensionality of the output (1 for regression /
            binary logit).
        learning_rate: Adam learning rate (must be > 0).
        epochs: Number of training epochs.
        batch_size: Mini-batch size.
        device: Device to run on — ``auto``, ``cpu``, or ``cuda``.
        seed: Random seed for reproducibility.
        shadow_only: When ``True`` (default) the run is marked shadow and
            ``promotion_eligible`` is forced to ``False``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_dim: int
    seq_len: int
    patch_len: int = 16
    stride: int = 8
    d_model: int = 64
    n_heads: int = 4
    n_layers: int = 2
    ff_dim: int = 128
    dropout: float = 0.1
    output_dim: int = 1
    learning_rate: float = 0.001
    epochs: int = 10
    batch_size: int = 32
    device: str = "auto"
    seed: int = 42
    shadow_only: bool = True

    @field_validator("input_dim")
    @classmethod
    def _input_dim_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"input_dim must be >= 1; got {v}")
        return v

    @field_validator("seq_len")
    @classmethod
    def _seq_len_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"seq_len must be >= 1; got {v}")
        return v

    @field_validator("patch_len")
    @classmethod
    def _patch_len_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"patch_len must be >= 1; got {v}")
        return v

    @field_validator("stride")
    @classmethod
    def _stride_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"stride must be >= 1; got {v}")
        return v

    @field_validator("d_model")
    @classmethod
    def _d_model_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"d_model must be >= 1; got {v}")
        return v

    @field_validator("n_heads")
    @classmethod
    def _n_heads_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"n_heads must be >= 1; got {v}")
        return v

    @field_validator("n_layers")
    @classmethod
    def _n_layers_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"n_layers must be >= 1; got {v}")
        return v

    @field_validator("ff_dim")
    @classmethod
    def _ff_dim_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"ff_dim must be >= 1; got {v}")
        return v

    @field_validator("output_dim")
    @classmethod
    def _output_dim_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"output_dim must be >= 1; got {v}")
        return v

    @field_validator("learning_rate")
    @classmethod
    def _learning_rate_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"learning_rate must be > 0; got {v}")
        return v

    @field_validator("epochs")
    @classmethod
    def _epochs_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"epochs must be >= 0; got {v}")
        return v

    @field_validator("batch_size")
    @classmethod
    def _batch_size_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"batch_size must be >= 1; got {v}")
        return v

    @field_validator("dropout")
    @classmethod
    def _dropout_range(cls, v: float) -> float:
        if not 0.0 <= v < 1.0:
            raise ValueError(f"dropout must be in [0, 1); got {v}")
        return v

    @field_validator("device")
    @classmethod
    def _device_allowed(cls, v: str) -> str:
        allowed = {"auto", "cpu", "cuda"}
        if v not in allowed:
            raise ValueError(f"device must be one of {sorted(allowed)}; got {v!r}")
        return v

    @model_validator(mode="after")
    def _patch_len_le_seq_len(self) -> PatchTSTConfig:
        """patch_len must be <= seq_len."""
        if self.patch_len > self.seq_len:
            raise ValueError(
                f"patch_len must be <= seq_len; "
                f"got patch_len={self.patch_len}, seq_len={self.seq_len}"
            )
        return self

    @model_validator(mode="after")
    def _d_model_divisible_by_n_heads(self) -> PatchTSTConfig:
        """d_model must be divisible by n_heads."""
        if self.d_model % self.n_heads != 0:
            raise ValueError(
                f"d_model must be divisible by n_heads; "
                f"got d_model={self.d_model}, n_heads={self.n_heads}"
            )
        return self

    def num_patches(self) -> int:
        """Return the number of patches produced by the patch embedding.

        The number of patches is the count of patch starting positions
        ``i`` with ``0 <= i <= seq_len - patch_len`` stepping by ``stride``.
        This is ``1 + (seq_len - patch_len) // stride`` and is always
        ``>= 1`` because ``patch_len <= seq_len`` is enforced.
        """
        return 1 + (self.seq_len - self.patch_len) // self.stride


class PatchTSTTrainingResult(BaseModel):
    """Result of a PatchTST canary training run.

    Frozen + ``extra='forbid'`` for audit integrity. Carries the config
    used, the per-epoch losses, the GPU status at training time, the
    paths to the saved model / OOF artifacts (if any), and the
    promotion-eligibility flag.

    Attributes:
        config: The :class:`PatchTSTConfig` used for the run.
        final_loss: The mean loss of the final epoch (NaN if 0 epochs).
        epoch_losses: Mean loss per epoch (one entry per epoch).
        gpu_status: GPU availability + memory state at training time.
        artifact_path: Path to the saved model state_dict, or ``None``.
        oof_artifact_path: Path to the OOF predictions artifact, or
            ``None`` if no OOF predictions were written.
        is_shadow: ``True`` when the run was in shadow mode.
        promotion_eligible: ``False`` when ``is_shadow`` is ``True``;
            only ``True`` for non-shadow runs (or shadow runs that later
            pass :func:`validate_promotion_eligibility` with a manual
            override).
        metrics: Extra metrics (e.g. ``{"mse": ...}``).
        duration_seconds: Wall-clock training duration in seconds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config: PatchTSTConfig
    final_loss: float
    epoch_losses: list[float] = Field(default_factory=list)
    gpu_status: GPUStatus
    artifact_path: str | None = None
    oof_artifact_path: str | None = None
    is_shadow: bool
    promotion_eligible: bool
    metrics: dict[str, float] = Field(default_factory=dict)
    duration_seconds: float


# ---------------------------------------------------------------------------
# PatchTST model
# ---------------------------------------------------------------------------


class PatchEmbedding:
    """Patch embedding layer for PatchTST.

    Patches the input sequence into contiguous segments of length
    ``patch_len`` (stepping by ``stride``), linearly projects each patch
    to ``d_model`` dimensions, and adds a learned positional encoding.

    Input shape: ``(batch, seq_len, input_dim)``.
    Output shape: ``(batch, num_patches, d_model)``.

    This is a thin wrapper around ``torch.nn.Module`` (built lazily so the
    module remains importable without torch), mirroring the pattern in
    :class:`~quant_foundry.tabular_neural_runtime.TinyTabularNet`.
    """

    def __init__(
        self,
        seq_len: int,
        patch_len: int,
        stride: int,
        input_dim: int,
        d_model: int,
        dropout: float = 0.1,
    ) -> None:
        if seq_len < 1:
            raise ValueError("seq_len must be >= 1")
        if patch_len < 1:
            raise ValueError("patch_len must be >= 1")
        if stride < 1:
            raise ValueError("stride must be >= 1")
        if input_dim < 1:
            raise ValueError("input_dim must be >= 1")
        if d_model < 1:
            raise ValueError("d_model must be >= 1")
        if patch_len > seq_len:
            raise ValueError("patch_len must be <= seq_len")
        if not 0.0 <= dropout < 1.0:
            raise ValueError("dropout must be in [0, 1)")
        self.seq_len = seq_len
        self.patch_len = patch_len
        self.stride = stride
        self.input_dim = input_dim
        self.d_model = d_model
        self.dropout = dropout
        self.num_patches = 1 + (seq_len - patch_len) // stride
        self._module: Any = None

    def _build_module(self) -> Any:
        """Build and return the underlying ``torch.nn.Module``.

        Lazily imports torch. The built module is cached on
        ``self._module`` so repeated calls return the same instance.
        """
        if self._module is not None:
            return self._module

        net = _make_patch_embedding_module_class()(
            seq_len=self.seq_len,
            patch_len=self.patch_len,
            stride=self.stride,
            input_dim=self.input_dim,
            d_model=self.d_model,
            num_patches=self.num_patches,
            dropout=self.dropout,
        )
        self._module = net
        return net

    @property
    def module(self) -> Any:
        """Return the underlying ``torch.nn.Module``, building it if needed."""
        return self._build_module()

    def forward(self, x: Any) -> Any:
        """Run a forward pass.

        Returns a tensor of shape ``(batch, num_patches, d_model)``.
        """
        return self.module(x)

    def parameters(self) -> Any:
        """Return the underlying module's parameters iterator."""
        return self.module.parameters()

    def state_dict(self) -> dict[str, Any]:
        """Return the underlying module's state_dict."""
        return cast("dict[str, Any]", self.module.state_dict())

    def load_state_dict(self, state_dict: dict[str, Any]) -> None:
        """Load a state_dict into the underlying module."""
        self.module.load_state_dict(state_dict)

    def to(self, device: Any) -> PatchEmbedding:
        """Move the underlying module to ``device`` and return self."""
        self._module = self.module.to(device)
        return self

    def train(self, mode: bool = True) -> PatchEmbedding:
        """Set the underlying module's train/eval mode and return self."""
        self.module.train(mode)
        return self

    def eval(self) -> PatchEmbedding:
        """Set the underlying module to eval mode and return self."""
        return self.train(False)


def _make_patch_embedding_module_class() -> Any:
    """Build and return the real ``nn.Module`` subclass for PatchEmbedding.

    Lazily imports torch. Called from
    :meth:`PatchEmbedding._build_module` each time a new embedding is built.
    """
    import torch
    import torch.nn as nn

    class _PatchEmbeddingNet(nn.Module):  # type: ignore[misc]  # torch nn.Module is Any when torch not installed
        """Inner nn.Module implementing the patch embedding forward pass."""

        def __init__(
            self,
            seq_len: int,
            patch_len: int,
            stride: int,
            input_dim: int,
            d_model: int,
            num_patches: int,
            dropout: float,
        ) -> None:
            super().__init__()
            self.seq_len = seq_len
            self.patch_len = patch_len
            self.stride = stride
            self.input_dim = input_dim
            self.d_model = d_model
            self.num_patches = num_patches
            # Linear projection: each flattened patch (patch_len * input_dim)
            # -> d_model.
            self.proj = nn.Linear(patch_len * input_dim, d_model)
            # Learned positional encoding (one row per patch).
            self.pos_embedding = nn.Parameter(torch.randn(1, num_patches, d_model) * 0.02)
            self.dropout = nn.Dropout(dropout)

        def forward(self, x: Any) -> Any:
            # x: (batch, seq_len, input_dim)
            batch = x.shape[0]
            # Extract patches via a list comprehension (keeps the logic
            # explicit and stride-flexible). Each patch is
            # (batch, patch_len, input_dim).
            patches: list[Any] = []
            for i in range(self.num_patches):
                start = i * self.stride
                end = start + self.patch_len
                patches.append(x[:, start:end, :])
            # Stack along a new patch dimension -> (batch, num_patches,
            # patch_len, input_dim) then flatten the last two dims.
            stacked = torch.stack(patches, dim=1)
            flattened = stacked.reshape(batch, self.num_patches, self.patch_len * self.input_dim)
            # Linear projection -> (batch, num_patches, d_model).
            projected = self.proj(flattened)
            # Add positional encoding (broadcast over batch).
            out = projected + self.pos_embedding
            return self.dropout(out)

    return _PatchEmbeddingNet


class PatchTSTModel:
    """A PatchTST model with a patch embedding, transformer encoder, and head.

    Architecture:

    - A :class:`PatchEmbedding` patches the input sequence into
      ``num_patches`` tokens of dimension ``d_model``.
    - A standard ``nn.TransformerEncoder`` with ``n_layers`` layers and
      ``n_heads`` attention heads processes the patch tokens.
    - A linear output head maps the mean-pooled patch representation to
      ``output_dim``.
    - The forward pass takes ``(batch, seq_len, input_dim)`` and returns
      ``(batch, output_dim)``.

    This is a thin wrapper around ``torch.nn.Module`` (built lazily so the
    module remains importable without torch), mirroring the pattern in
    :class:`~quant_foundry.tabm_trainer.TabMModel`.
    """

    def __init__(
        self,
        input_dim: int,
        seq_len: int,
        patch_len: int,
        stride: int,
        d_model: int,
        n_heads: int,
        n_layers: int,
        ff_dim: int,
        output_dim: int = 1,
        dropout: float = 0.1,
    ) -> None:
        if input_dim < 1:
            raise ValueError("input_dim must be >= 1")
        if seq_len < 1:
            raise ValueError("seq_len must be >= 1")
        if patch_len < 1:
            raise ValueError("patch_len must be >= 1")
        if stride < 1:
            raise ValueError("stride must be >= 1")
        if d_model < 1:
            raise ValueError("d_model must be >= 1")
        if n_heads < 1:
            raise ValueError("n_heads must be >= 1")
        if n_layers < 1:
            raise ValueError("n_layers must be >= 1")
        if ff_dim < 1:
            raise ValueError("ff_dim must be >= 1")
        if output_dim < 1:
            raise ValueError("output_dim must be >= 1")
        if patch_len > seq_len:
            raise ValueError("patch_len must be <= seq_len")
        if d_model % n_heads != 0:
            raise ValueError("d_model must be divisible by n_heads")
        if not 0.0 <= dropout < 1.0:
            raise ValueError("dropout must be in [0, 1)")
        self.input_dim = input_dim
        self.seq_len = seq_len
        self.patch_len = patch_len
        self.stride = stride
        self.d_model = d_model
        self.n_heads = n_heads
        self.n_layers = n_layers
        self.ff_dim = ff_dim
        self.output_dim = output_dim
        self.dropout = dropout
        self.num_patches = 1 + (seq_len - patch_len) // stride
        self._module: Any = None

    def _build_module(self) -> Any:
        """Build and return the underlying ``torch.nn.Module``.

        Lazily imports torch. The built module is cached on
        ``self._module`` so repeated calls return the same instance.
        """
        if self._module is not None:
            return self._module

        import torch.nn as nn

        patch_embedding = PatchEmbedding(
            seq_len=self.seq_len,
            patch_len=self.patch_len,
            stride=self.stride,
            input_dim=self.input_dim,
            d_model=self.d_model,
            dropout=self.dropout,
        )
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=self.d_model,
            nhead=self.n_heads,
            dim_feedforward=self.ff_dim,
            dropout=self.dropout,
            batch_first=True,
        )
        encoder = nn.TransformerEncoder(encoder_layer, num_layers=self.n_layers)
        head = nn.Linear(self.d_model, self.output_dim)

        net = _make_patchtst_module_class()(
            patch_embedding=patch_embedding.module,
            encoder=encoder,
            head=head,
        )
        self._module = net
        return net

    @property
    def module(self) -> Any:
        """Return the underlying ``torch.nn.Module``, building it if needed."""
        return self._build_module()

    def forward(self, x: Any) -> Any:
        """Run a forward pass.

        Returns a tensor of shape ``(batch, output_dim)``.
        """
        return self.module(x)

    def parameters(self) -> Any:
        """Return the underlying module's parameters iterator."""
        return self.module.parameters()

    def state_dict(self) -> dict[str, Any]:
        """Return the underlying module's state_dict."""
        return cast("dict[str, Any]", self.module.state_dict())

    def load_state_dict(self, state_dict: dict[str, Any]) -> None:
        """Load a state_dict into the underlying module."""
        self.module.load_state_dict(state_dict)

    def to(self, device: Any) -> PatchTSTModel:
        """Move the underlying module to ``device`` and return self."""
        self._module = self.module.to(device)
        return self

    def train(self, mode: bool = True) -> PatchTSTModel:
        """Set the underlying module's train/eval mode and return self."""
        self.module.train(mode)
        return self

    def eval(self) -> PatchTSTModel:
        """Set the underlying module to eval mode and return self."""
        return self.train(False)


def _make_patchtst_module_class() -> Any:
    """Build and return the real ``nn.Module`` subclass for PatchTST.

    Lazily imports torch. Called from
    :meth:`PatchTSTModel._build_module` each time a new model is built.
    """
    import torch.nn as nn

    class _PatchTSTNet(nn.Module):  # type: ignore[misc]  # torch nn.Module is Any when torch not installed
        """Inner nn.Module implementing the PatchTST forward pass."""

        def __init__(
            self,
            patch_embedding: nn.Module,
            encoder: nn.Module,
            head: nn.Module,
        ) -> None:
            super().__init__()
            self.patch_embedding = patch_embedding
            self.encoder = encoder
            self.head = head

        def forward(self, x: Any) -> Any:
            # x: (batch, seq_len, input_dim)
            # patches: (batch, num_patches, d_model)
            patches = self.patch_embedding(x)
            # Transformer encoder over patch tokens.
            encoded = self.encoder(patches)
            # Mean-pool over the patch dimension -> (batch, d_model).
            pooled = encoded.mean(dim=1)
            # Output head -> (batch, output_dim).
            return self.head(pooled)

    return _PatchTSTNet


# ---------------------------------------------------------------------------
# Device resolution
# ---------------------------------------------------------------------------


def _resolve_device(config: PatchTSTConfig, gpu_status: GPUStatus) -> Any:
    """Resolve the torch device for a PatchTST run.

    ``auto`` picks CUDA when available, else CPU. ``cpu`` / ``cuda`` are
    honored literally (``cuda`` on a CPU-only host falls back to CPU).
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


# ---------------------------------------------------------------------------
# PatchTSTTrainer
# ---------------------------------------------------------------------------


class PatchTSTTrainer:
    """Train / predict / save / load / OOF-write façade for PatchTST.

    The trainer builds a :class:`PatchTSTModel`, trains it with Adam on
    the provided sequence features / labels, saves the trained
    state_dict, and can write OOF predictions via :class:`OOFWriter`.

    Args:
        config: The :class:`PatchTSTConfig` for the run.
    """

    def __init__(self, config: PatchTSTConfig) -> None:
        if not isinstance(config, PatchTSTConfig):
            raise TypeError("config must be a PatchTSTConfig")
        self.config = config
        self.model_: PatchTSTModel | None = None

    # -- training ---------------------------------------------------------

    def train(
        self,
        X: Any,
        y: Any,
        weights: Any = None,
    ) -> PatchTSTTrainingResult:
        """Train a PatchTST model on ``X`` / ``y`` and return the result.

        Args:
            X: Sequence features — a numpy array or list of lists of
                shape ``(n_samples, seq_len, input_dim)``.
            y: Labels — a 1-D array-like or a pandas Series.
            weights: Optional sample weights (1-D array-like). When
                provided, the per-batch loss is weighted by them.

        Returns:
            A :class:`PatchTSTTrainingResult` with epoch losses, GPU
            status, and promotion eligibility.
        """
        import numpy as np
        import torch
        import torch.nn as nn

        start = time.perf_counter()

        torch.manual_seed(self.config.seed)
        np.random.seed(self.config.seed)

        gpu_status = check_gpu()
        device = _resolve_device(self.config, gpu_status)

        X_arr = np.array(X, dtype=float)
        if X_arr.ndim != 3:
            raise ValueError(
                f"X must be 3-D (n_samples, seq_len, input_dim); got shape {X_arr.shape}"
            )
        if X_arr.shape[1] != self.config.seq_len:
            raise ValueError(
                f"X.shape[1] must equal seq_len={self.config.seq_len}; got {X_arr.shape[1]}"
            )
        if X_arr.shape[2] != self.config.input_dim:
            raise ValueError(
                f"X.shape[2] must equal input_dim={self.config.input_dim}; got {X_arr.shape[2]}"
            )
        y_arr = np.array(y, dtype=float).reshape(-1, 1)
        w_arr = (
            np.array(weights, dtype=float)
            if weights is not None
            else np.ones(y_arr.shape[0], dtype=float)
        )

        n_samples = X_arr.shape[0]
        x_tensor = torch.from_numpy(X_arr).float()
        y_tensor = torch.from_numpy(y_arr).float()
        w_tensor = torch.from_numpy(w_arr).float()

        model = PatchTSTModel(
            input_dim=self.config.input_dim,
            seq_len=self.config.seq_len,
            patch_len=self.config.patch_len,
            stride=self.config.stride,
            d_model=self.config.d_model,
            n_heads=self.config.n_heads,
            n_layers=self.config.n_layers,
            ff_dim=self.config.ff_dim,
            output_dim=self.config.output_dim,
            dropout=self.config.dropout,
        )
        model.to(device)
        model.train()

        nn.MSELoss()
        optimizer = torch.optim.Adam(
            model.parameters(),
            lr=self.config.learning_rate,
        )

        epoch_losses: list[float] = []
        final_loss = float("nan")

        if self.config.epochs > 0 and n_samples > 0:
            for _epoch in range(self.config.epochs):
                permutation = torch.randperm(n_samples)
                batch_losses: list[float] = []
                for i in range(0, n_samples, self.config.batch_size):
                    idx = permutation[i : i + self.config.batch_size]
                    batch_x = x_tensor[idx].to(device)
                    batch_y = y_tensor[idx].to(device)
                    batch_w = w_tensor[idx].to(device)

                    # TransformerEncoderLayer with batchnorm requires
                    # more than 1 sample per batch.
                    if batch_x.shape[0] < 2:
                        continue

                    optimizer.zero_grad()
                    preds = model.forward(batch_x)
                    # preds: (batch, output_dim)
                    # Weighted MSE.
                    per_sample = (preds - batch_y) ** 2
                    # Mean over output_dim -> (batch,).
                    per_sample = per_sample.mean(dim=-1)
                    loss = (per_sample * batch_w).sum() / batch_w.sum().clamp(min=1e-8)
                    loss.backward()
                    optimizer.step()
                    batch_losses.append(float(loss.item()))

                if batch_losses:
                    epoch_loss = float(sum(batch_losses) / len(batch_losses))
                else:
                    epoch_loss = float("nan")
                epoch_losses.append(epoch_loss)

            if epoch_losses:
                final_loss = epoch_losses[-1]

        self.model_ = model

        # Compute a simple MSE metric on the training set (eval mode).
        metrics: dict[str, float] = {}
        if n_samples > 0 and self.config.epochs > 0:
            model.eval()
            with torch.no_grad():
                preds = model.forward(x_tensor.to(device))
                preds_np = preds.cpu().numpy()
                mse = float(np.mean((preds_np - y_arr) ** 2))
                metrics["mse"] = mse
                metrics["final_loss"] = final_loss

        duration = time.perf_counter() - start

        return PatchTSTTrainingResult(
            config=self.config,
            final_loss=final_loss,
            epoch_losses=epoch_losses,
            gpu_status=gpu_status,
            artifact_path=None,
            oof_artifact_path=None,
            is_shadow=self.config.shadow_only,
            promotion_eligible=not self.config.shadow_only,
            metrics=metrics,
            duration_seconds=duration,
        )

    # -- prediction -------------------------------------------------------

    def predict(self, X: Any) -> list[float]:
        """Predict outputs for ``X``.

        Uses the in-memory trained model (or the model loaded via
        :meth:`load_artifact`), runs a forward pass, and returns a 1-D
        list of predictions.

        Args:
            X: Sequence features — same format as :meth:`train`.

        Returns:
            A list of floats (one prediction per sample).
        """
        import numpy as np
        import torch

        if self.model_ is None:
            raise ValueError("no trained model available — call train() or load_artifact() first")

        X_arr = np.array(X, dtype=float)
        if X_arr.ndim != 3:
            raise ValueError(
                f"X must be 3-D (n_samples, seq_len, input_dim); got shape {X_arr.shape}"
            )
        x_tensor = torch.from_numpy(X_arr).float()

        model = self.model_
        model.eval()
        gpu_status = check_gpu()
        device = _resolve_device(self.config, gpu_status)
        model.to(device)

        with torch.no_grad():
            preds = model.forward(x_tensor.to(device))
            preds_np = preds.cpu().numpy()

        return [float(v) for v in preds_np.reshape(-1)]

    # -- artifact persistence ---------------------------------------------

    def save_artifact(self, path: str) -> None:
        """Save the trained model's state_dict to ``path``.

        Creates parent directories as needed. The file is written via
        ``torch.save`` (state_dict only — no pickled module).

        Raises:
            ValueError: if no model has been trained.
        """
        import torch

        if self.model_ is None:
            raise ValueError("no trained model to save — call train() first")
        p = Path(path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        torch.save(self.model_.state_dict(), str(p))

    def load_artifact(self, path: str) -> PatchTSTModel:
        """Load a state_dict from ``path`` into a new :class:`PatchTSTModel`.

        The new model is built from the trainer's config and the saved
        state_dict is loaded into it. The model is set to eval mode on
        CPU and stored on the trainer (``self.model_``).

        Returns:
            The loaded :class:`PatchTSTModel`.
        """
        import torch

        model = PatchTSTModel(
            input_dim=self.config.input_dim,
            seq_len=self.config.seq_len,
            patch_len=self.config.patch_len,
            stride=self.config.stride,
            d_model=self.config.d_model,
            n_heads=self.config.n_heads,
            n_layers=self.config.n_layers,
            ff_dim=self.config.ff_dim,
            output_dim=self.config.output_dim,
            dropout=self.config.dropout,
        )
        # Build the underlying module before loading state_dict.
        _ = model.module
        state_dict = torch.load(str(path), map_location="cpu")
        model.load_state_dict(state_dict)
        model.eval()
        self.model_ = model
        return model

    # -- OOF writing ------------------------------------------------------

    def write_oof_predictions(
        self,
        fold_predictions: list[list[float]],
        fold_ids: list[int],
        symbols: list[str],
        timestamps: list[str],
        labels: list[float],
        horizons: list[int],
        weights: list[float] | None,
        output_path: str,
    ) -> str:
        """Write OOF predictions for ensemble integration.

        Uses :class:`OOFWriter` from :mod:`quant_foundry.oof_artifacts`
        to write the predictions in the standard OOF artifact schema so
        they can be merged with other model families' OOF predictions
        for stacking.

        Args:
            fold_predictions: Per-row predictions. Must have the same
                length as ``fold_ids``.
            fold_ids: Per-row fold ids.
            symbols: Per-row instrument symbols.
            timestamps: Per-row ISO-format timestamps.
            labels: Per-row ground-truth labels.
            horizons: Per-row prediction horizons.
            weights: Per-row sample weights. When ``None``, 1.0 is
                used for every row.
            output_path: Path to write the OOF artifact to. The file is
                named ``oof_patchtst.json`` in the parent directory of
                this path (the parent directory is the OOF output dir).

        Returns:
            The path to the written OOF artifact file.
        """
        n = len(fold_predictions)
        if not (
            len(fold_ids) == n
            and len(symbols) == n
            and len(timestamps) == n
            and len(labels) == n
            and len(horizons) == n
        ):
            raise ValueError(
                "fold_predictions, fold_ids, symbols, timestamps, "
                "labels, and horizons must all have the same length"
            )
        if weights is not None and len(weights) != n:
            raise ValueError("weights must have the same length as fold_predictions or be None")

        output_dir = str(Path(output_path).parent)
        writer = OOFWriter(model_family="patchtst", output_dir=output_dir)
        for i in range(n):
            row_id = f"{symbols[i]}_{timestamps[i]}_{horizons[i]}"
            w = float(weights[i]) if weights is not None else 1.0
            writer.add_prediction(
                row_id=row_id,
                fold_id=int(fold_ids[i]),
                symbol=str(symbols[i]),
                timestamp=str(timestamps[i]),
                label=float(labels[i]),
                prediction=float(cast("float", fold_predictions[i])),
                horizon=int(horizons[i]),
                weight=w,
            )
        artifact = writer.flush()
        return artifact.artifact_uri


# ---------------------------------------------------------------------------
# Promotion eligibility
# ---------------------------------------------------------------------------


def validate_promotion_eligibility(
    result: PatchTSTTrainingResult,
    manual_override: bool = False,
) -> bool:
    """Validate whether a PatchTST training result is promotion eligible.

    Promotion rules (fail-closed):

    - If ``result.is_shadow`` is ``True`` (the default for PatchTST), the
      run is **only** eligible when ``manual_override`` is ``True`` —
      i.e. an operator explicitly overrides the shadow gate. A shadow
      run with no override is **not** eligible.
    - If ``result.is_shadow`` is ``False`` (a non-shadow run), the run is
      eligible regardless of ``manual_override``.

    Args:
        result: The :class:`PatchTSTTrainingResult` to validate.
        manual_override: When ``True``, override the shadow gate and
            mark a shadow run as eligible.

    Returns:
        ``True`` if the result is promotion eligible, ``False``
        otherwise.
    """
    if result.is_shadow and not manual_override:
        return False
    if manual_override:
        return True
    # Not shadow and no override -> eligible.
    return True


# ---------------------------------------------------------------------------
# Family registration helper
# ---------------------------------------------------------------------------


def register_patchtst_family() -> dict[str, Any]:
    """Return a ``ModelFamilySpec``-compatible dict for PatchTST registration.

    The returned dict carries the fields a
    :class:`~quant_foundry.alpha_genome.ModelFamilySpec` expects
    (family_id, display_name, version, dataset_shape, objectives,
    artifact_format, artifact_loader, required_metrics, etc.) plus
    PatchTST-specific metadata. It is intended to be passed to
    ``ModelFamilyRegistry.register`` (after wrapping in a
    ``ModelFamilySpec``) by the caller — this function does **not**
    mutate the registry itself, keeping this module file-disjoint from
    ``alpha_genome.py``.

    The spec marks PatchTST as a shadow family: it is **not** a baseline
    exception, does not require a GPU (the trainer degrades gracefully
    to CPU), and defaults to the ``CHALLENGER`` promotion-eligibility
    class (though the trainer itself forces ``promotion_eligible=False``
    when ``shadow_only=True``).
    """
    return {
        "family_id": "patchtst",
        "display_name": "PatchTST (shadow canary)",
        "version": "1",
        "dataset_shape": "sequence_windowed",
        "objectives": ("binary", "regression"),
        "artifact_format": "torch_state_dict",
        "artifact_loader": "quant_foundry.patchtst_trainer.PatchTSTTrainer.load_artifact",
        "required_metrics": ("mse", "mae", "final_loss"),
        "runpod_image": None,
        "requires_gpu": False,
        "max_budget_cents": 0,
        "promotion_eligibility_class": "challenger",
        "is_baseline_exception": False,
        "created_at_ns": time.time_ns(),
        "shadow_only": True,
        "default_patch_len": 16,
        "default_stride": 8,
        "default_d_model": 64,
        "default_n_heads": 4,
        "default_n_layers": 2,
    }


__all__ = [
    "PatchEmbedding",
    "PatchTSTConfig",
    "PatchTSTModel",
    "PatchTSTTrainer",
    "PatchTSTTrainingResult",
    "register_patchtst_family",
    "validate_promotion_eligibility",
]
